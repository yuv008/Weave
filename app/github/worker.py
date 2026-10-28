"""Poll issues and pull requests for one GitHub repository."""

import logging
import time
from datetime import UTC, datetime

from app.github.client import GitHubRequestError
from app.github.normalize import normalize_github_comment, normalize_github_issue
from app.graph.policy import allowed_source, github_source_id
from app.graph.upsert import ensure_indexes
from app.ingest.chunks import index_source
from app.graph.upsert_document import upsert_document
from app.ingest.log import format_github_log
from app.ingest.records import SYNC_FAILURE, SYNC_STATE, open_store
from app.ingest.state import failure_attempts, record_failure, write_connection
from app.ingest.time import sortable_timestamp
from app.models.canonical_document import CanonicalDocument
from shared.ids import document_id, github_issue_id

logger = logging.getLogger("weave.github")
_MAX_FAILURES = 5
_EMPTY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_STORED = """
MATCH (n {id: $id})
RETURN n.version, n.kind, n.status, n.author_external_id, n.parent_external_id, n.source_url
"""


def github_sync_id(tenant_id: str, repo_id: str) -> str:
    return f"{tenant_id}:github:sync:{repo_id}"


def github_comment_sync_id(tenant_id: str, repo_id: str, number: str) -> str:
    return f"{tenant_id}:github:sync:{repo_id}:issue:{number}"


def github_failure_id(tenant_id: str, repo_id: str, external_id: str) -> str:
    return f"{tenant_id}:github:failure:{repo_id}:{external_id}"


def poll_repository(tenant_id: str, connection_id: str, owner: str, repo: str, client, graph, store=None) -> None:
    found = allowed_source("github", f"{owner}/{repo}", tenant_id)
    if found is None:
        return
    group, _policy = found
    records = open_store(store)
    ensure_indexes(graph)
    repo_payload = client.get_repo(owner, repo).payload
    repo_value = repo_payload.get("id") if isinstance(repo_payload, dict) else None
    if isinstance(repo_value, bool) or not isinstance(repo_value, int):
        raise ValueError("GitHub repository id is required")
    repo_id = str(repo_payload["id"])
    write_connection(
        graph,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=repo_id,
        source="github",
    )
    sync_id = github_sync_id(tenant_id, repo_id)
    state = _read(records, sync_id)
    page = state["page"] or "1"
    since = state["since"]
    greatest = since
    while True:
        started = time.perf_counter()
        try:
            listed = client.list_issues(owner, repo, page, since or None)
        except GitHubRequestError as exc:
            _log(tenant_id, repo_id, 0, 1, started, exc.error)
            return
        issues = listed.payload if isinstance(listed.payload, list) else []
        failed = False
        accepted: list[dict] = []
        try:
            for raw in issues:
                if not isinstance(raw, dict):
                    _remember(records, tenant_id, connection_id, repo_id, owner, repo, "", "missing", "issue", "ValueError")
                    failed = True
                    break
                if not _same_listed_repo(raw, repo_id):
                    continue
                outcome = _ingest_issue(raw, tenant_id, connection_id, repo_id, owner, repo, graph, records)
                if outcome == "blocked":
                    failed = True
                    break
                if outcome == "ok":
                    accepted.append(raw)
                    greatest = _later(greatest, raw.get("updated_at"))
            if not failed:
                for raw in accepted:
                    blocked, seen = _sync_comments(
                        raw,
                        tenant_id=tenant_id,
                        connection_id=connection_id,
                        repo_id=repo_id,
                        owner=owner,
                        repo=repo,
                        client=client,
                        graph=graph,
                        store=records,
                    )
                    greatest = _later(greatest, seen)
                    if blocked:
                        failed = True
                        break
        except GitHubRequestError as exc:
            _log(tenant_id, repo_id, len(issues), 1, started, exc.error)
            return
        except Exception as exc:
            _log(tenant_id, repo_id, len(issues), 1, started, type(exc).__name__)
            raise
        if failed:
            _log(tenant_id, repo_id, len(issues), 1, started, "ValueError")
            return
        finished = listed.next_page == ""
        _write(
            records,
            sync_id,
            {
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "workspace_id": repo_id,
                "owner": owner,
                "repo": repo,
                "page": "1" if finished else listed.next_page,
                "since": greatest if finished and greatest else since,
                "status": "complete" if finished else "in_progress",
                "last_error_class": "",
            },
        )
        source = github_source_id(tenant_id, f"{owner}/{repo}")
        records.note_source_sync(tenant_id, source, group)
        index_source(graph, records, source)
        _log(tenant_id, repo_id, len(issues), 0, started, "")
        if finished:
            return
        page = listed.next_page


def retry_github_failures(graph, client, store=None) -> None:
    records = open_store(store)
    rows = []
    for row in records.list_kind(SYNC_FAILURE):
        if ":github:failure:" not in row["id"]:
            continue
        payload = row["payload"]
        if int(payload.get("attempts") or 0) >= 5:
            continue
        rows.append(
            (
                row["id"],
                payload.get("tenant_id"),
                payload.get("connection_id"),
                payload.get("repo_id"),
                payload.get("owner"),
                payload.get("repo"),
                payload.get("number"),
                payload.get("external_id"),
                payload.get("kind"),
                payload.get("node_id"),
            )
        )
    for node_id, tenant_id, connection_id, repo_id, owner, repo, number, external_id, kind, _document_id in rows:
        try:
            if kind == "comment":
                found = _find_comment(client, owner, repo, str(number), external_id)
                if found is None:
                    if _tombstone_if_stored(graph, tenant_id, connection_id, repo_id, external_id, owner, repo, kind):
                        _delete_failure(records, node_id)
                    else:
                        _remember(
                            records, tenant_id, connection_id, repo_id, owner, repo,
                            str(number), external_id, kind, "404",
                        )
                    continue
                document = normalize_github_comment(
                    found,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    repo_id=repo_id,
                    issue_number=number,
                    owner=owner or "",
                    repo=repo or "",
                )
            else:
                payload = client.get_issue(owner, repo, str(number)).payload
                document = normalize_github_issue(
                    payload,
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    repo_id=repo_id,
                    owner=owner or "",
                    repo=repo or "",
                    kind="pull_request" if kind == "pull_request" else None,
                )
            upsert_document(graph, document)
        except GitHubRequestError as exc:
            if exc.status == 404 and _tombstone_if_stored(
                graph, tenant_id, connection_id, repo_id, external_id, owner, repo, kind or "issue"
            ):
                _delete_failure(records, node_id)
                continue
            _remember(
                records, tenant_id, connection_id, repo_id, owner, repo,
                str(number or ""), external_id or "", kind or "issue", str(exc.status),
            )
            continue
        except ValueError:
            _remember(
                records, tenant_id, connection_id, repo_id, owner, repo,
                str(number or ""), external_id or "", kind or "issue", "ValueError",
            )
            continue
        _delete_failure(records, node_id)


def _sync_comments(raw, *, tenant_id, connection_id, repo_id, owner, repo, client, graph, store) -> tuple[bool, str]:
    number = str(raw.get("number"))
    sync_id = github_comment_sync_id(tenant_id, repo_id, number)
    state = _read_comments(store, sync_id)
    page = "1" if state["status"] == "complete" else (state["comments_page"] or "1")
    since = state["comments_since"]
    greatest = since
    while True:
        started = time.perf_counter()
        listed = client.list_comments(owner, repo, number, page, since or None)
        comments = listed.payload if isinstance(listed.payload, list) else []
        blocked = False
        for comment in comments:
            if not isinstance(comment, dict):
                _remember(
                    store, tenant_id, connection_id, repo_id, owner, repo,
                    number, "comment:missing", "comment", "ValueError",
                )
                blocked = True
                break
            outcome = _ingest_comment(
                comment, tenant_id, connection_id, repo_id, owner, repo, number, graph, store,
            )
            if outcome == "blocked":
                blocked = True
                break
            if outcome == "ok":
                greatest = _later(greatest, comment.get("updated_at"))
        if blocked:
            _log(tenant_id, repo_id, len(comments), 1, started, "ValueError")
            return True, greatest
        finished = listed.next_page == ""
        _write_comments(
            store,
            sync_id,
            {
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "workspace_id": repo_id,
                "owner": owner,
                "repo": repo,
                "number": number,
                "comments_page": "1" if finished else listed.next_page,
                "comments_since": greatest if finished and greatest else since,
                "status": "complete" if finished else "in_progress",
            },
        )
        _log(tenant_id, repo_id, len(comments), 0, started, "")
        if finished:
            return False, greatest
        page = listed.next_page


def _ingest_issue(raw, tenant_id, connection_id, repo_id, owner, repo, graph, store) -> str:
    number = raw.get("number")
    external_id = f"issue:{number}" if isinstance(number, int) and not isinstance(number, bool) else "missing"
    failure = github_failure_id(tenant_id, repo_id, external_id)
    if failure_attempts(store, failure) >= _MAX_FAILURES:
        return "skipped"
    try:
        document = normalize_github_issue(
            raw,
            tenant_id=tenant_id,
            connection_id=connection_id,
            repo_id=repo_id,
            owner=owner,
            repo=repo,
        )
    except ValueError:
        _remember(store, tenant_id, connection_id, repo_id, owner, repo, str(number or ""), external_id, "issue", "ValueError")
        return "blocked"
    upsert_document(graph, document)
    _delete_failure(store, failure)
    return "ok"


def _ingest_comment(raw, tenant_id, connection_id, repo_id, owner, repo, number, graph, store) -> str:
    comment_id = raw.get("id")
    external_id = f"comment:{comment_id}" if isinstance(comment_id, int) and not isinstance(comment_id, bool) else "comment:missing"
    failure = github_failure_id(tenant_id, repo_id, external_id)
    if failure_attempts(store, failure) >= _MAX_FAILURES:
        return "skipped"
    try:
        document = normalize_github_comment(
            raw,
            tenant_id=tenant_id,
            connection_id=connection_id,
            repo_id=repo_id,
            issue_number=number,
            owner=owner,
            repo=repo,
        )
    except ValueError:
        _remember(store, tenant_id, connection_id, repo_id, owner, repo, str(number), external_id, "comment", "ValueError")
        return "blocked"
    upsert_document(graph, document)
    _delete_failure(store, failure)
    return "ok"


def _find_comment(client, owner, repo, number, external_id):
    target = external_id.split(":", 1)[1] if isinstance(external_id, str) and ":" in external_id else ""
    page = "1"
    while True:
        listed = client.list_comments(owner, repo, number, page, None)
        comments = listed.payload if isinstance(listed.payload, list) else []
        for comment in comments:
            if isinstance(comment, dict) and str(comment.get("id")) == target:
                return comment
        if not listed.next_page:
            return None
        page = listed.next_page


def _tombstone_if_stored(graph, tenant_id, connection_id, repo_id, external_id, owner, repo, kind) -> bool:
    if not external_id:
        return False
    node_id = _github_node_id(tenant_id, owner, repo, external_id, kind)
    rows = graph.query(_STORED, {"id": node_id}).result_set
    if not rows:
        return False
    version, kind, status, author, parent, source_url = rows[0]
    stamped = version or sortable_timestamp(datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ"))
    if not str(stamped).endswith("~deleted"):
        stamped = f"{stamped}~deleted"
    parent_id = parent or None
    now = datetime.now(UTC)
    upsert_document(
        graph,
        CanonicalDocument(
            tenant_id=tenant_id,
            connection_id=connection_id,
            source="github",
            workspace_id=str(repo_id),
            external_id=external_id,
            occurred_at=now,
            updated_at=now,
            version=stamped,
            deleted=True,
            source_url=source_url or "",
            content_hash=_EMPTY_HASH,
            kind=kind if kind in {"issue", "pull_request", "comment"} else "",
            status=status if status in {"open", "closed", "merged"} else "",
            author_external_id=author or "",
            parent_external_id=parent_id,
            repo_slug=f"{owner}/{repo}" if owner and repo else "",
        ),
    )
    return True


def _same_listed_repo(raw: dict, repo_id: str) -> bool:
    repository = raw.get("repository")
    if not isinstance(repository, dict) or "id" not in repository:
        return True
    return str(repository.get("id")) == repo_id


def _remember(store, tenant_id, connection_id, repo_id, owner, repo, number, external_id, kind, error_class) -> None:
    record_failure(
        store,
        node_id=github_failure_id(tenant_id, repo_id, external_id or "missing"),
        fields={
            "node_id": _github_node_id(tenant_id, owner, repo, external_id, kind),
            "version": "",
            "error_class": error_class,
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "repo_id": repo_id,
            "owner": owner,
            "repo": repo,
            "number": str(number or ""),
            "external_id": external_id or "",
            "kind": kind,
        },
    )


def _github_node_id(tenant_id: str, owner: str, repo: str, external_id: str, kind: str) -> str:
    if not owner or not repo or not external_id or external_id in {"missing", "comment:missing"}:
        return ""
    slug = f"{owner}/{repo}"
    if kind in {"issue", "pull_request"} and external_id.startswith("issue:"):
        return github_issue_id(tenant_id, slug, int(external_id.split(":", 1)[1]))
    if kind == "comment":
        return document_id(tenant_id, "github", slug, external_id)
    return ""


def _delete_failure(store, node_id: str) -> None:
    store.delete(node_id)


def _read(store, sync_id: str) -> dict[str, str]:
    row = store.get(sync_id)
    payload = row["payload"] if row else {}
    return {
        "page": payload.get("page") or "",
        "since": payload.get("since") or "",
        "status": payload.get("status") or "",
    }


def _write(store, sync_id: str, fields: dict[str, str]) -> None:
    store.merge(sync_id, fields.get("tenant_id", ""), SYNC_STATE, fields)


def _read_comments(store, sync_id: str) -> dict[str, str]:
    row = store.get(sync_id)
    payload = row["payload"] if row else {}
    return {
        "comments_page": payload.get("comments_page") or "",
        "comments_since": payload.get("comments_since") or "",
        "status": payload.get("status") or "",
    }


def _write_comments(store, sync_id: str, fields: dict[str, str]) -> None:
    store.merge(sync_id, fields.get("tenant_id", ""), SYNC_STATE, fields)


def _later(current: str, candidate: object) -> str:
    if not isinstance(candidate, str) or not candidate:
        return current
    try:
        stamped = sortable_timestamp(candidate)
    except ValueError:
        return current
    if not current or stamped > current:
        return stamped
    return current


def _log(tenant_id: str, repo_id: str, records: int, failed: int, started: float, error_class: str) -> None:
    logger.info(
        "%s",
        format_github_log(
            tenant_id=tenant_id,
            repo_id=repo_id,
            records=records,
            failed=failed,
            duration_ms=int((time.perf_counter() - started) * 1000),
            error_class=error_class,
        ),
    )
