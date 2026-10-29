"""Full text is chunked once per new version, and only chunks with no vector are embedded."""

import hashlib
from datetime import UTC, datetime

from app.graph.upsert import upsert_message
from app.ingest.chunks import BODY_LIMIT, CHUNK_CHARS, embed_pending, split_text, write_item_chunks
from app.ingest.records import MemoryStore
from app.models.canonical import CanonicalMessage
from app.slack.normalize import format_slack_version


def _counting_embedder():
    calls = []

    def embed(texts):
        calls.append(list(texts))
        return [[float(len(text)), 1.0] for text in texts]

    return embed, calls


def test_split_text_cuts_at_paragraphs_overlaps_and_covers_everything():
    paragraphs = [f"Paragraph {n}. " + "word " * 60 for n in range(12)]
    text = "\n\n".join(paragraphs)
    pieces = split_text(text)

    assert len(pieces) > 1
    assert all(len(piece) <= CHUNK_CHARS for piece in pieces)
    assert all(piece.startswith("Paragraph") or piece[:1].isalpha() for piece in pieces)
    assert "Paragraph 0." in pieces[0] and "Paragraph 11." in pieces[-1]
    assert split_text("  short  ") == ["short"]
    assert split_text("") == []


def test_an_item_keeps_only_its_current_chunks_and_a_deleted_item_keeps_none():
    store = MemoryStore()
    common = {"tenant_id": "north", "item_id": "north:doc", "acl_groups": ["notion:w", "org:all"], "min_level": 0}

    assert write_item_chunks(store, body="x " * 2000, deleted=False, **common) > 2
    assert write_item_chunks(store, body="now short", deleted=False, **common) == 1
    assert [row["text"] for row in store._chunks.values()] == ["now short"]
    assert next(iter(store._chunks.values()))["acl_groups"] == ["notion:w", "org:all"]

    write_item_chunks(store, body="gone", deleted=True, **common)
    assert store._chunks == {}


def test_a_huge_body_is_capped():
    store = MemoryStore()
    write_item_chunks(store, tenant_id="north", item_id="north:big", body="y " * BODY_LIMIT,
                      acl_groups=["notion:w"], min_level=0, deleted=False)
    assert sum(len(row["text"]) for row in store._chunks.values()) <= BODY_LIMIT * 1.2


def test_only_pending_chunks_are_embedded_and_only_for_that_company():
    store = MemoryStore()
    embed, calls = _counting_embedder()
    for tenant in ("north", "south"):
        write_item_chunks(store, tenant_id=tenant, item_id=f"{tenant}:a", body="hello", acl_groups=["slack:C1"],
                          min_level=0, deleted=False)

    assert embed_pending(store, "north", embedder=embed) == 1
    assert embed_pending(store, "north", embedder=embed) == 0
    assert calls == [["hello"]]
    assert store._chunks["south:a:chunk:0"]["embedding"] is None
    assert embed_pending(MemoryStore(), "north") == 0


def test_replaying_the_same_slack_version_does_not_rechunk(monkeypatch):
    class Graph:
        def __init__(self, applied):
            self.applied = applied

        def query(self, query, params=None):
            class R:
                result_set = [[self.applied]] if "RETURN do_apply" in query else []

            return R()

    writes = []
    monkeypatch.setattr("app.ingest.chunks.write_item_chunks", lambda store, **kw: writes.append(kw["item_id"]))
    message = CanonicalMessage(
        tenant_id="tenant1", connection_id="conn1", workspace_id="T1", external_id="C0ENGPUB:1.000001",
        conversation_external_id="C0ENGPUB", occurred_at=datetime(2026, 1, 1, tzinfo=UTC),
        version=format_slack_version("1.000001"), excerpt="hi", body="hi there",
        content_hash=hashlib.sha256(b"hi there").hexdigest(),
    )
    upsert_message(Graph(True), message, store=MemoryStore())
    upsert_message(Graph(False), message, store=MemoryStore())
    upsert_message(Graph(True), message)

    assert writes == [message.node_id]
