import hashlib
import uuid
from datetime import UTC, datetime

import pytest

from app.models.canonical import CanonicalMessage
from app.graph.upsert import upsert_message
from app.slack.normalize import format_slack_version

TOKEN = 'UNIQUE_EXCERPT_TOKEN "quoted"'


class _Result:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    def __init__(self, first_row):
        self.calls = []
        self._first_row = first_row

    def query(self, query, params=None):
        self.calls.append((query, params or {}))
        if "RETURN do_apply" in query:
            return _Result(self._first_row)
        return _Result([])


def _message(**overrides) -> CanonicalMessage:
    fields = {
        "tenant_id": "tenant1",
        "connection_id": "conn1",
        "workspace_id": "T1",
        "external_id": "C0ENGSSO:1403051575.000407",
        "conversation_external_id": "C0ENGSSO",
        "author_external_id": "U1",
        "occurred_at": datetime(2014, 6, 18, 1, 39, 35, 407, tzinfo=UTC),
        "version": format_slack_version("1403051575.000407"),
        "excerpt": "hello",
        "content_hash": hashlib.sha256(b"hello").hexdigest(),
    }
    fields.update(overrides)
    return CanonicalMessage(**fields)


def test_excerpt_is_sent_only_as_a_query_parameter():
    graph = FakeGraph([[True]])
    message = _message(excerpt=TOKEN)

    result = upsert_message(graph, message)

    assert result.applied is True
    assert graph.calls
    for query, params in graph.calls:
        assert TOKEN not in query
        assert "$excerpt" in graph.calls[0][0]
    assert graph.calls[0][1]["excerpt"] == TOKEN


def test_stale_write_does_not_link_edges():
    graph = FakeGraph([])
    result = upsert_message(graph, _message())

    assert result.applied is False
    assert len(graph.calls) == 1


def _falkor_graph():
    pytest.importorskip("falkordb")
    from shared.env import load_env
    from shared.graphdb import connect

    load_env()
    try:
        client = connect()
        name = f"weave_test_{uuid.uuid4().hex}"
        graph = client.select_graph(name)
        graph.query("RETURN 1")
    except Exception as exc:
        pytest.skip(f"FalkorDB is not available: {type(exc).__name__}")
    from app.graph.upsert import ensure_indexes

    ensure_indexes(graph)
    ensure_indexes(graph)
    yield graph
    graph.delete()


@pytest.fixture
def graph():
    yield from _falkor_graph()


def _rows(graph, query, params):
    return graph.query(query, params).result_set


def _properties(graph, node_id):
    rows = _rows(
        graph,
        """
        MATCH (m:Message {id: $id})
        RETURN m.excerpt, m.version, m.placeholder, m.deleted
        """,
        {"id": node_id},
    )
    assert len(rows) == 1
    return (*rows[0], len(rows))


def test_replaying_a_message_keeps_a_single_node_and_edge(graph):
    message = _message()
    assert upsert_message(graph, message).applied is True
    assert upsert_message(graph, message).applied is True

    excerpt, version, placeholder, deleted, count = _properties(graph, message.node_id)
    assert excerpt == "hello"
    assert version == message.version
    assert placeholder is False
    assert deleted is False
    assert count == 1
    in_source = _rows(
        graph,
        """
        MATCH (m:Message {id: $id})-[r:IN_SOURCE]->(:Conversation)
        RETURN count(r)
        """,
        {"id": message.node_id},
    )
    assert in_source[0][0] == 1


def test_older_version_does_not_overwrite(graph):
    current = _message(
        excerpt="current",
        version=format_slack_version("1403051576.000000"),
        content_hash=hashlib.sha256(b"current").hexdigest(),
    )
    older = _message(excerpt="older")
    upsert_message(graph, current)
    assert upsert_message(graph, older).applied is False

    excerpt, version, _, _, _ = _properties(graph, current.node_id)
    assert excerpt == "current"
    assert version == current.version


def test_equal_version_refreshes_edges_without_changing_text(graph):
    first = _message(excerpt="first")
    replay = _message(
        excerpt="second",
        content_hash=hashlib.sha256(b"second").hexdigest(),
    )
    upsert_message(graph, first)
    assert upsert_message(graph, replay).applied is True

    excerpt, _, _, _, _ = _properties(graph, first.node_id)
    assert excerpt == "first"


def test_reply_before_parent_creates_a_placeholder_that_is_filled(graph):
    parent = _message(excerpt="parent body")
    reply = _message(
        external_id="C0ENGSSO:1403051576.000100",
        author_external_id="U2",
        excerpt="reply body",
        version=format_slack_version("1403051576.000100"),
        occurred_at=datetime(2014, 6, 18, 1, 39, 36, 100, tzinfo=UTC),
        thread_parent_external_id=parent.external_id,
        content_hash=hashlib.sha256(b"reply body").hexdigest(),
    )

    upsert_message(graph, reply)
    placeholder_excerpt, placeholder_version, placeholder, _, _ = _properties(
        graph, parent.node_id
    )
    assert placeholder is True
    assert placeholder_version == ""
    assert placeholder_excerpt == ""

    upsert_message(graph, parent)
    excerpt, version, placeholder, _, count = _properties(graph, parent.node_id)
    assert excerpt == "parent body"
    assert version == parent.version
    assert placeholder is False
    assert count == 1

    replies = _rows(
        graph,
        """
        MATCH (reply:Message {id: $reply_id})-[:REPLIES_TO]->(parent:Message {id: $parent_id})
        RETURN count(reply)
        """,
        {"reply_id": reply.node_id, "parent_id": parent.node_id},
    )
    author_id = _rows(
        graph,
        """
        MATCH (m:Message {id: $reply_id})
        RETURN m.author_external_id
        """,
        {"reply_id": reply.node_id},
    )
    assert replies[0][0] == 1
    assert author_id[0][0] == "U2"


def _acl_written(graph) -> list:
    return graph.calls[0][1]["acl_groups"]


def test_an_org_wide_source_writes_org_all_so_the_whole_company_can_read_it():
    graph = FakeGraph([[True]])
    upsert_message(graph, _message(external_id="C0ENGPUB:1403051575.000407", conversation_external_id="C0ENGPUB"))

    assert _acl_written(graph) == ["slack:C0ENGPUB", "org:all"]


def test_a_source_that_is_not_org_wide_keeps_only_its_own_group():
    graph = FakeGraph([[True]])
    upsert_message(graph, _message())

    assert _acl_written(graph) == ["slack:C0ENGSSO"]


def test_a_member_reads_an_org_wide_repo_item_but_not_a_team_repo_item():
    from agents.gate.rules import item_is_visible, person_groups
    from app.graph.upsert_document import upsert_document
    from app.models.canonical_document import CanonicalDocument
    from shared.schema import Prop

    def written(repo):
        graph = FakeGraph([[True]])
        now = datetime(2026, 10, 29, tzinfo=UTC)
        upsert_document(graph, CanonicalDocument(
            tenant_id="tenant1", connection_id="conn1", source="github", workspace_id="1",
            external_id="issue:1", occurred_at=now, updated_at=now, version="v1",
            source_url="", content_hash=hashlib.sha256(b"").hexdigest(), kind="issue",
            repo_slug=repo,
        ))
        return graph.calls[0][1]["acl_groups"]

    member = person_groups("tenant1:person:bo@north.test", 0, {})
    public = written("demo-org/roadmap")
    team = written("demo-org/platform")
    fresh = {"github:demo-org/roadmap", "github:demo-org/platform"}

    assert item_is_visible({Prop.MIN_LEVEL: 0, Prop.ACL_GROUPS: public}, 0, member, fresh)
    assert not item_is_visible({Prop.MIN_LEVEL: 0, Prop.ACL_GROUPS: team}, 0, member, fresh)
