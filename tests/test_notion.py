import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.answer.notion_question import answer_notion_question
from app.graph.upsert_document import upsert_document
from app.models.canonical_document import CanonicalDocument
from app.notion.blocks import plain_text_from_blocks
from app.notion.client import NotionClient, NotionRequestError
from app.notion.normalize import normalize_notion_page
from app.ingest.records import MemoryStore
from app.notion.worker import notion_sync_id, poll_shared_pages

TOKEN = "secret-notion-token"


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers, data=None):
        self.calls.append((url, headers, data))
        status, body, header = self.responses.pop(0)
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return status, header, payload


def _client(responses):
    transport = Transport(responses)
    sleeps = []
    client = NotionClient(TOKEN, transport=transport, sleep=sleeps.append, random_fn=lambda: 0.0)
    return client, transport, sleeps


def _page(page_id="page-1", edited="2026-03-11T00:00:00.000Z", **extra):
    payload = {
        "object": "page",
        "id": page_id,
        "created_time": "2026-01-01T00:00:00.000Z",
        "last_edited_time": edited,
        "url": f"https://www.notion.so/{page_id}",
        "properties": {"title": {"type": "title", "title": [{"plain_text": "Plan"}]}},
    }
    payload.update(extra)
    return payload


def test_notion_client_sends_version_and_retries_rate_limit():
    client, transport, sleeps = _client(
        [
            (429, {"code": "rate_limited"}, {"Retry-After": "1"}),
            (200, {"object": "user"}, {}),
        ]
    )
    assert client.users_me()["object"] == "user"
    assert sleeps == [1.0]
    url, headers, _body = transport.calls[0]
    assert headers["Notion-Version"] == "2026-03-11"
    assert TOKEN not in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_notion_4xx_is_not_retried():
    client, transport, sleeps = _client([(404, {"code": "object_not_found"}, {})])
    with pytest.raises(NotionRequestError) as caught:
        client.retrieve_page("missing")
    assert caught.value.error == "object_not_found"
    assert sleeps == []
    assert len(transport.calls) == 1


def test_blocks_keep_supported_text_only():
    text = plain_text_from_blocks(
        [
            {"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": "Annual"}]}},
            {"type": "image", "image": {}},
            {"type": "heading_1", "heading_1": {"rich_text": [{"plain_text": "Plan"}]}},
        ]
    )
    assert text == "Annual\nPlan"


def test_trashed_page_drops_the_body():
    secret = "do not store this page body"
    document = normalize_notion_page(
        _page(in_trash=True),
        secret,
        tenant_id="demo",
        connection_id="notion-main",
        workspace_id="ws",
    )
    assert document is not None
    assert document.deleted is True
    assert document.excerpt == ""
    assert secret not in document.model_dump_json()
    assert document.node_id == "demo:notion:document:ws:page-1"


def test_database_objects_are_skipped():
    assert (
        normalize_notion_page(
            {"object": "database", "id": "db"},
            "",
            tenant_id="demo",
            connection_id="notion-main",
            workspace_id="ws",
        )
        is None
    )


def _document(**overrides) -> CanonicalDocument:
    fields = {
        "tenant_id": "demo",
        "connection_id": "notion-main",
        "source": "notion",
        "workspace_id": "sales-deals",
        "external_id": "page-1",
        "title": "Plan",
        "occurred_at": datetime(2026, 1, 1, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 11, tzinfo=UTC),
        "version": "2026-03-11T00:00:00.000000Z",
        "excerpt": "Annual plan",
        "source_url": "https://www.notion.so/page-1",
        "content_hash": hashlib.sha256(b"Annual plan").hexdigest(),
    }
    fields.update(overrides)
    return CanonicalDocument(**fields)


def test_document_upsert_is_idempotent_and_rejects_older_versions(graph):
    current = _document()
    assert upsert_document(graph, current).applied is True
    assert upsert_document(graph, current).applied is True
    older = _document(
        excerpt="older",
        version="2026-01-01T00:00:00.000000Z",
        content_hash=hashlib.sha256(b"older").hexdigest(),
    )
    assert upsert_document(graph, older).applied is False
    rows = graph.query(
        "MATCH (d:Document {id: $id}) RETURN d.excerpt",
        {"id": current.node_id},
    ).result_set
    assert rows[0][0] == "Annual plan"
    edge = graph.query(
        "MATCH (:Connection)-[r:HAS_SOURCE]->(:Document {id: $id}) RETURN count(r)",
        {"id": current.node_id},
    ).result_set
    assert edge[0][0] == 1


def test_equal_version_keeps_the_excerpt(graph):
    upsert_document(graph, _document(excerpt="first"))
    upsert_document(
        graph,
        _document(excerpt="second", content_hash=hashlib.sha256(b"second").hexdigest()),
    )
    excerpt = graph.query(
        "MATCH (d:Document {id: $id}) RETURN d.excerpt",
        {"id": _document().node_id},
    ).result_set[0][0]
    assert excerpt == "first"


class _Search:
    def __init__(self):
        self.stop = True
        self.calls = []

    def search_pages(self, cursor=None, filter_last_edited_time_after=None):
        self.calls.append(cursor)
        if cursor == "page-2":
            if self.stop:
                raise RuntimeError("stop after page 1")
            return {"results": [_page("page-2", "2026-03-10T00:00:00.000Z")], "has_more": False}
        return {"results": [_page()], "has_more": True, "next_cursor": "page-2"}

    def retrieve_page(self, page_id):
        edited = "2026-03-11T00:00:00.000Z" if page_id == "page-1" else "2026-03-10T00:00:00.000Z"
        return _page(page_id, edited)

    def block_children(self, block_id, cursor=None):
        return {
            "results": [{"type": "paragraph", "paragraph": {"rich_text": [{"plain_text": block_id}]}}],
            "has_more": False,
        }


def test_notion_checkpoint_survives_a_crash_and_replay_does_not_duplicate(graph):
    client = _Search()
    store = MemoryStore()
    with pytest.raises(RuntimeError, match="stop after page 1"):
        poll_shared_pages("demo", "notion-main", "sales-deals", client, graph, store)
    payload = store.get(notion_sync_id("demo", "sales-deals"))["payload"]
    assert payload.get("search_cursor") == "page-2"
    assert payload.get("last_edited_after") in ("", None)
    assert payload.get("status") == "in_progress"
    client.stop = False
    poll_shared_pages("demo", "notion-main", "sales-deals", client, graph, store)
    count = graph.query(
        "MATCH (d:Document) WHERE d.tenant_id = 'demo' AND d.source = 'notion' RETURN count(d)"
    ).result_set[0][0]
    assert count == 2
    calls = len(client.calls)
    poll_shared_pages("demo", "notion-main", "sales-deals", client, graph, store)
    assert len(client.calls) > calls
    assert (
        graph.query(
            "MATCH (d:Document) WHERE d.tenant_id = 'demo' AND d.source = 'notion' RETURN count(d)"
        ).result_set[0][0]
        == 2
    )


def test_notion_answer_skips_a_deleted_document(graph):
    live = _document(excerpt="annual plan stays", external_id="live", source_url="https://www.notion.so/live")
    deleted = _document(
        excerpt="",
        external_id="gone",
        deleted=True,
        content_hash=hashlib.sha256(b"").hexdigest(),
    )
    upsert_document(graph, live)
    upsert_document(graph, deleted)
    graph.query(
        "MATCH (d:Document {id: $id}) SET d.excerpt = 'annual plan secret'",
        {"id": deleted.node_id},
    )
    answer = answer_notion_question(graph, "demo", "annual plan")
    assert len(answer.citations) == 1
    assert answer.citations[0].source_url == "https://www.notion.so/live"
    assert "annual plan secret" not in answer.text


def test_page_text_reads_nested_toggles_code_and_tables_but_not_child_pages():
    from app.notion.worker import _page_text

    def para(text, block_id=None, children=False, kind="paragraph"):
        return {"id": block_id or text, "type": kind, "has_children": children,
                kind: {"rich_text": [{"plain_text": text}]}}

    tree = {
        "page": [
            para("Intro"),
            para("Details", block_id="toggle-1", children=True, kind="toggle"),
            {"id": "sub", "type": "child_page", "has_children": True, "child_page": {"title": "Sub"}},
            {"id": "t", "type": "table", "has_children": True, "table": {}},
        ],
        "toggle-1": [para("print('hi')", kind="code"), para("Deep", block_id="d1", children=True)],
        "d1": [para("Deeper", block_id="d2", children=True)],
        "d2": [para("Deepest", block_id="d3", children=True)],
        "d3": [para("Too deep")],
        "t": [{"id": "r", "type": "table_row", "has_children": False,
               "table_row": {"cells": [[{"plain_text": "Q4"}], [{"plain_text": "$2M"}]]}}],
        "sub": [para("Belongs to the child page")],
    }

    class Client:
        def block_children(self, block_id, cursor=None):
            return {"results": tree[block_id], "has_more": False}

    text = _page_text(Client(), "page")

    for expected in ("Intro", "Details", "print('hi')", "Deepest", "Q4 | $2M"):
        assert expected in text
    assert "Too deep" not in text
    assert "Belongs to the child page" not in text
