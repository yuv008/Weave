import hashlib
import hmac
import json
from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.answer.github_question import answer_github_question
from app.github.client import PINNED_GITHUB_API_VERSION, GitHubClient, GitHubRequestError, GitHubResult
from app.github.normalize import normalize_github_comment, normalize_github_issue
from app.github.worker import github_failure_id, github_sync_id, poll_repository, retry_github_failures
from app.ingest.records import SYNC_FAILURE, MemoryStore
from app.graph.upsert_document import upsert_document
from app.ingest.log import format_github_log
from app.ingest.time import sortable_timestamp
from app.main import create_app
from app.models.canonical_document import document_node_id
from shared.ids import github_issue_id

TOKEN = "github-test-token"
SECRET = "hook-secret"


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        status, body, header = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return status, header, payload


def _client(responses):
    transport = Transport(responses)
    sleeps = []
    client = GitHubClient(
        TOKEN,
        transport=transport,
        sleep=sleeps.append,
        random_fn=lambda: 0.0,
        now=lambda: 1_700_000_000,
    )
    return client, transport, sleeps


def _issue(number, updated, repo=99, title="Ship", body="the plan", state="open", pull=None, merged_at=None):
    payload = {
        "number": number,
        "updated_at": updated,
        "created_at": updated,
        "title": title,
        "body": body,
        "state": state,
        "html_url": f"https://github.com/octo/hello/issues/{number}",
        "user": {"login": "ada"},
        "repository": {"id": repo},
    }
    if pull is not None:
        payload["pull_request"] = pull
    if merged_at is not None:
        payload["merged_at"] = merged_at
    return payload


def _comment(comment_id, number, updated, body):
    return {
        "id": comment_id,
        "updated_at": updated,
        "created_at": updated,
        "body": body,
        "html_url": f"https://github.com/octo/hello/issues/{number}#issuecomment-{comment_id}",
        "user": {"login": "ada"},
    }


def test_github_list_retries_rate_limit_and_keeps_the_token_out_of_the_url():
    link = '<https://api.github.com/repos/octo/hello/issues?page=2>; rel="next"'
    client, transport, sleeps = _client(
        [
            (403, {"message": "API rate limit exceeded"}, {"x-ratelimit-remaining": "0", "retry-after": "0"}),
            (200, [_issue(1, "2026-03-11T00:00:00Z")], {"link": link}),
        ]
    )
    page = client.list_issues("octo", "hello", "1", None)
    assert page.next_page == "2"
    assert page.payload[0]["number"] == 1
    assert sleeps == [0]
    url, headers = transport.calls[0]
    assert TOKEN not in url
    assert "/repos/octo/hello/issues?" in url
    assert "/contents" not in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["X-GitHub-Api-Version"] == PINNED_GITHUB_API_VERSION
    assert headers["User-Agent"] == "weave"


def test_github_403_and_404_are_not_retried():
    client, transport, sleeps = _client([(403, {"message": "Resource not accessible"}, {})])
    with pytest.raises(GitHubRequestError) as caught:
        client.get_repo("octo", "hello")
    assert caught.value.status == 403
    assert sleeps == []
    assert len(transport.calls) == 1
    client, transport, sleeps = _client([(404, {"message": "Not Found"}, {})])
    with pytest.raises(GitHubRequestError) as missing:
        client.get_issue("octo", "hello", "9")
    assert missing.value.status == 404
    assert sleeps == []
    assert len(transport.calls) == 1


def test_issue_pull_request_and_comment_normalization():
    issue = normalize_github_issue(
        _issue(4, "2026-03-11T00:00:00Z", pull=None),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    assert issue is not None
    assert issue.kind == "issue"
    assert issue.status == "open"
    assert issue.node_id == "demo:github:issue:demo-org/sso-service:4"
    pull = normalize_github_issue(
        _issue(5, "2026-03-11T00:00:00Z", state="closed", pull={"merged_at": "2026-03-11T01:00:00Z"}),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    assert pull.kind == "pull_request"
    assert pull.status == "merged"
    closed = normalize_github_issue(
        _issue(6, "2026-03-11T00:00:00Z", state="closed"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    assert closed.status == "closed"
    assert closed.deleted is False
    tail = "x" * 400
    long = normalize_github_issue(
        _issue(7, "2026-03-11T00:00:00Z", title="Title", body=tail),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    assert len(long.excerpt) == 280
    assert tail not in long.model_dump_json()
    assert long.content_hash == hashlib.sha256(f"Title\n{tail}".encode()).hexdigest()
    missing_user = dict(_issue(8, "2026-03-11T00:00:00Z"))
    missing_user["user"] = None
    assert normalize_github_issue(missing_user, tenant_id="demo", connection_id="github-main", repo_id="99").author_external_id == ""
    comment = normalize_github_comment(
        _comment(10, 4, "2026-03-11T02:00:00Z", "because of the outage"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
        issue_number=4,
    )
    assert comment.kind == "comment"
    assert comment.parent_external_id == "issue:4"
    assert comment.node_id == "demo:github:document:demo-org/sso-service:comment:10"
    deleted = normalize_github_issue(
        _issue(4, "2026-03-11T03:00:00Z"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
        deleted=True,
    )
    assert deleted.deleted is True
    assert deleted.excerpt == ""
    assert deleted.version.endswith("~deleted")
    with pytest.raises(ValueError):
        normalize_github_issue({"updated_at": "2026-03-11T00:00:00Z"}, tenant_id="demo", connection_id="github-main", repo_id="99")


def test_comment_before_issue_and_stale_write(graph):
    comment = normalize_github_comment(
        _comment(10, 4, "2026-03-11T02:00:00Z", "because of the outage"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
        issue_number=4,
    )
    upsert_document(graph, comment)
    parent_id = github_issue_id("demo", "demo-org/sso-service", 4)
    placeholder, version = graph.query(
        "MATCH (d:Issue {id: $id}) RETURN d.placeholder, d.version",
        {"id": parent_id},
    ).result_set[0]
    assert placeholder is True
    assert version == ""
    issue = normalize_github_issue(
        _issue(4, "2026-03-11T03:00:00Z", title="Ship", body="the plan"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    upsert_document(graph, issue)
    excerpt, placeholder = graph.query(
        "MATCH (d:Issue {id: $id}) RETURN d.excerpt, d.placeholder",
        {"id": parent_id},
    ).result_set[0]
    assert excerpt.startswith("Ship")
    assert placeholder is False
    assert graph.query("MATCH ()-[r:REPLIES_TO]->() RETURN count(r)").result_set[0][0] == 1
    graph.query("MATCH ()-[r:REPLIES_TO]->() DELETE r")
    upsert_document(graph, comment)
    assert graph.query("MATCH ()-[r:REPLIES_TO]->() RETURN count(r)").result_set[0][0] == 1
    older = normalize_github_comment(
        _comment(10, 4, "2026-03-11T01:00:00Z", "stale"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
        issue_number=4,
    )
    graph.query("MATCH ()-[r:REPLIES_TO]->() DELETE r")
    upsert_document(graph, older)
    stored = graph.query(
        "MATCH (d:Document {id: $id}) RETURN d.excerpt",
        {"id": comment.node_id},
    ).result_set[0][0]
    assert stored == "because of the outage"
    assert graph.query("MATCH ()-[r:REPLIES_TO]->() RETURN count(r)").result_set[0][0] == 0
    deleted = normalize_github_issue(
        _issue(4, "2026-03-12T00:00:00Z"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
        deleted=True,
    )
    upsert_document(graph, comment)
    upsert_document(graph, deleted)
    assert graph.query("MATCH (d:Issue {id: $id}) RETURN d.excerpt", {"id": parent_id}).result_set[0][0] == ""
    assert graph.query("MATCH ()-[r:HAS_SOURCE]->() RETURN count(r)").result_set[0][0] >= 1
    assert graph.query("MATCH ()-[r:REPLIES_TO]->() RETURN count(r)").result_set[0][0] == 1


class _Repo:
    def __init__(self):
        self.stop = True
        self.issue_calls = []

    def get_repo(self, owner, repo):
        assert (owner, repo) == ("demo-org", "sso-service")
        return GitHubResult({"id": 99, "private": True}, "")

    def list_issues(self, owner, repo, page, since):
        self.issue_calls.append((str(page), since or ""))
        if str(page) == "2":
            return GitHubResult([_issue(2, "2026-03-10T00:00:00Z")], "")
        return GitHubResult(
            [_issue(1, "2026-03-11T00:00:00Z"), _issue(3, "2026-03-09T00:00:00Z", repo=5)],
            "2",
        )

    def list_comments(self, owner, repo, number, page, since):
        if str(number) == "3":
            raise AssertionError("foreign repo")
        if str(number) == "2":
            return GitHubResult([], "")
        if str(page) == "2":
            if self.stop:
                raise RuntimeError("stop after comment page 1")
            return GitHubResult([_comment(11, 1, "2026-03-11T01:00:00Z", "second page")], "")
        return GitHubResult([_comment(10, 1, "2026-03-11T00:30:00Z", "first page")], "2")


def _count(graph) -> int:
    return graph.query(
        """
        MATCH (d)
        WHERE (d:Issue OR d:Document) AND d.source = 'github' AND d.tenant_id = 'demo'
          AND coalesce(d.placeholder, false) = false
        RETURN count(d)
        """
    ).result_set[0][0]


def test_github_checkpoint_survives_a_crash_and_skips_another_repo(graph):
    client = _Repo()
    store = MemoryStore()
    with pytest.raises(RuntimeError, match="stop after comment page 1"):
        poll_repository("demo", "github-main", "demo-org", "sso-service", client, graph, store)
    assert store.get(github_sync_id("demo", "99")) is None
    assert _count(graph) == 2
    missing = document_node_id("demo", "github", "99", "issue:3")
    assert graph.query("MATCH (d:Document {id: $id}) RETURN d.id", {"id": missing}).result_set == []
    client.stop = False
    poll_repository("demo", "github-main", "demo-org", "sso-service", client, graph, store)
    assert _count(graph) == 4
    poll_repository("demo", "github-main", "demo-org", "sso-service", client, graph, store)
    assert _count(graph) == 4
    payload = store.get(github_sync_id("demo", "99"))["payload"]
    page, since, status = payload.get("page"), payload.get("since"), payload.get("status")
    assert page == "1"
    assert status == "complete"
    assert since == sortable_timestamp("2026-03-11T01:00:00Z")


def _seed_failure(store, external_id, number, attempts=1):
    store.merge(
        github_failure_id("demo", "99", external_id),
        "demo",
        SYNC_FAILURE,
        {
            "attempts": attempts,
            "tenant_id": "demo",
            "connection_id": "github-main",
            "repo_id": "99",
            "owner": "demo-org",
            "repo": "sso-service",
            "number": number,
            "external_id": external_id,
            "kind": "issue",
        },
    )


def test_retry_fetches_one_issue_without_the_list(graph):
    class Client:
        def __init__(self):
            self.calls = []

        def list_issues(self, *args):
            self.calls.append("list_issues")
            raise AssertionError("list")

        def get_issue(self, owner, repo, number):
            self.calls.append(("get_issue", number))
            return GitHubResult(_issue(4, "2026-03-11T00:00:00Z", body="recovered"), "")

        def list_comments(self, *args):
            self.calls.append("list_comments")
            raise AssertionError("comments")

    failure = github_failure_id("demo", "99", "issue:4")
    store = MemoryStore()
    _seed_failure(store, "issue:4", "4")
    client = Client()
    retry_github_failures(graph, client, store)
    assert client.calls == [("get_issue", "4")]
    assert store.get(failure) is None
    excerpt = graph.query(
        "MATCH (i:Issue {id: $id}) RETURN i.excerpt",
        {"id": github_issue_id("demo", "demo-org/sso-service", 4)},
    ).result_set[0][0]
    assert "recovered" in excerpt


def test_retry_404_tombstones_a_stored_issue_and_keeps_an_unknown_one(graph):
    issue = normalize_github_issue(
        _issue(4, "2026-03-11T00:00:00Z", body="still here"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    upsert_document(graph, issue)

    class Missing:
        def get_issue(self, owner, repo, number):
            raise GitHubRequestError(404, "Not Found")

        def list_issues(self, *args):
            raise AssertionError("list")

        def list_comments(self, *args):
            raise AssertionError("comments")

    stored = github_failure_id("demo", "99", "issue:4")
    unknown = github_failure_id("demo", "99", "issue:8")
    store = MemoryStore()
    _seed_failure(store, "issue:4", "4")
    _seed_failure(store, "issue:8", "8")
    retry_github_failures(graph, Missing(), store)
    assert graph.query("MATCH (i:Issue {id: $id}) RETURN i.excerpt, i.deleted", {"id": issue.node_id}).result_set[0] == ["", True]
    assert store.get(stored) is None
    assert store.get(unknown)["payload"]["attempts"] == 2
    assert graph.query(
        "MATCH (i:Issue {id: $id}) RETURN i.id",
        {"id": github_issue_id("demo", "demo-org/sso-service", 8)},
    ).result_set == []


def test_fifth_github_failure_stays(graph):
    failure = github_failure_id("demo", "99", "issue:5")
    store = MemoryStore()
    _seed_failure(store, "issue:5", "5", attempts=5)

    class Client:
        def get_issue(self, *args):
            raise AssertionError("fetched")

        def list_issues(self, *args):
            raise AssertionError("list")

        def list_comments(self, *args):
            raise AssertionError("comments")

    retry_github_failures(graph, Client(), store)
    assert store.get(failure)["payload"]["attempts"] == 5


def test_github_log_drops_title_and_token():
    title = "unique-issue-title"
    line = format_github_log(
        tenant_id="demo",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
        records=1,
        failed=0,
        duration_ms=5,
        error_class="",
        title=title,
        token=TOKEN,
    )
    assert line == "tenant_id=demo repo_id=99 records=1 failed=0 duration_ms=5 error_class="
    assert title not in line
    assert TOKEN not in line


def test_github_answer_skips_a_deleted_comment_and_keeps_a_closed_issue(graph):
    issue = normalize_github_issue(
        _issue(4, "2026-03-12T00:00:00Z", title="Ship", body="the plan", state="closed"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    comment = normalize_github_comment(
        _comment(10, 4, "2026-03-11T00:00:00Z", "the plan leaked"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
        issue_number=4,
        deleted=True,
    )
    upsert_document(graph, issue)
    upsert_document(graph, comment)
    graph.query(
        "MATCH (d:Document {id: $id}) SET d.excerpt = 'the plan leaked'",
        {"id": comment.node_id},
    )
    answer = answer_github_question(graph, "demo", "99", "the plan")
    assert len(answer.citations) == 1
    assert answer.citations[0].source_url == "https://github.com/octo/hello/issues/4"
    assert answer.citations[0].status == "closed"
    assert "leaked" not in answer.text


def _sign(body: bytes) -> str:
    digest = hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _hook(graph):
    return TestClient(
        create_app(
            graph=graph,
            tenant_id="demo",
            connection_id="slack",
            workspace_id="T1",
            github_secret=SECRET,
            github_owner="demo-org",
            github_repo="sso-service",
            github_connection_id="github-main",
        )
    )


def _post(client, payload, event, *, signature=None, delivery="delivery-1"):
    body = json.dumps(payload).encode()
    return client.post(
        "/github/webhook",
        content=body,
        headers={
            "content-type": "application/json",
            "x-github-event": event,
            "x-github-delivery": delivery,
            "x-hub-signature-256": _sign(body) if signature is None else signature,
        },
    )


def _repository():
    return {"id": 99, "name": "sso-service", "owner": {"login": "demo-org"}}


def test_bad_github_signature_does_not_touch_the_graph():
    class Recording:
        def __init__(self):
            self.calls = 0

        def query(self, query, params=None):
            self.calls += 1
            raise AssertionError(query)

    graph = Recording()
    response = _post(_hook(graph), {"action": "opened", "repository": _repository()}, "issues", signature="sha256=nope")
    assert response.status_code == 401
    assert graph.calls == 0


def test_delete_blanks_the_excerpt_and_keeps_contains(graph):
    issue = normalize_github_issue(
        _issue(7, "2026-03-11T00:00:00Z", body="keep me"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    comment = normalize_github_comment(
        _comment(12, 7, "2026-03-11T01:00:00Z", "a reply"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
        issue_number=7,
    )
    upsert_document(graph, issue)
    upsert_document(graph, comment)
    response = _post(
        _hook(graph),
        {"action": "deleted", "issue": _issue(7, "2026-03-11T00:00:00Z", body="keep me"), "repository": _repository()},
        "issues",
    )
    assert response.status_code == 200
    excerpt = graph.query("MATCH (d:Issue {id: $id}) RETURN d.excerpt", {"id": issue.node_id}).result_set[0][0]
    assert excerpt == ""
    assert graph.query("MATCH ()-[r:REPLIES_TO]->() RETURN count(r)").result_set[0][0] == 1


def test_older_github_edit_does_not_replace_a_newer_excerpt(graph):
    issue = normalize_github_issue(
        _issue(7, "2026-03-12T00:00:00Z", body="current decision"),
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    upsert_document(graph, issue)
    response = _post(
        _hook(graph),
        {
            "action": "edited",
            "issue": _issue(7, "2026-03-11T00:00:00Z", body="old decision"),
            "repository": _repository(),
        },
        "issues",
    )
    assert response.status_code == 200
    excerpt = graph.query("MATCH (d:Issue {id: $id}) RETURN d.excerpt", {"id": issue.node_id}).result_set[0][0]
    assert "current decision" in excerpt
    assert "old decision" not in excerpt
