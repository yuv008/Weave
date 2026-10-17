import os
from datetime import UTC, datetime

import pytest

from app.graph.client import connect_graph
from app.graph.upsert import upsert_message
from app.graph.upsert_document import upsert_document
from app.github.normalize import normalize_github_issue
from app.ingest.records import MemoryStore, PostgresStore
from app.ingest.state import event_already_applied, mark_event_applied, read_channel_sync, write_channel_sync
from app.models.canonical import CanonicalMessage
from app.slack.normalize import format_slack_version
from shared.ids import github_issue_id
from shared.schema import Rel


def _message(channel: str) -> CanonicalMessage:
    return CanonicalMessage(
        tenant_id="tenant1",
        connection_id="conn1",
        workspace_id="T1",
        external_id=f"{channel}:10.1",
        conversation_external_id=channel,
        author_external_id="U1",
        occurred_at=datetime(2026, 3, 11, tzinfo=UTC),
        version=format_slack_version("10.1"),
        excerpt="ship with provider A",
        content_hash="ab" * 32,
        source_url="https://acme.slack.com/archives/C0ENGSSO/p101",
    )


def test_unknown_channel_writes_nothing(graph):
    result = upsert_message(graph, _message("C1"))

    assert result.applied is False
    assert graph.query("MATCH (n) RETURN count(n)").result_set[0][0] == 0


def test_known_channel_adds_contract_labels_and_edges(graph):
    message = _message("C0ENGSSO")
    assert upsert_message(graph, message).applied is True

    labels = graph.query(
        "MATCH (m:Message:Item {id: $id}) RETURN labels(m)",
        {"id": message.node_id},
    ).result_set
    assert labels
    row = graph.query(
        f"""
        MATCH (m:Message {{id: $id}})-[:{Rel.IN_SOURCE}]->(s:Source)
        RETURN s.id, m.author_external_id
        """,
        {"id": message.node_id},
    ).result_set[0]
    assert row[1] == "U1"
    authored = graph.query(
        f"MATCH (m:Message {{id: $id}})-[:{Rel.AUTHORED_BY}]->() RETURN count(*)",
        {"id": message.node_id},
    ).result_set[0][0]
    assert authored == 0


def test_github_issue_id_matches_the_shared_builder(graph):
    issue = normalize_github_issue(
        {
            "number": 42,
            "updated_at": "2026-03-11T00:00:00Z",
            "created_at": "2026-03-11T00:00:00Z",
            "title": "SSO",
            "html_url": "https://github.com/demo-org/sso-service/issues/42",
            "user": {"login": "dev"},
        },
        tenant_id="demo",
        connection_id="github-main",
        repo_id="99",
        owner="demo-org",
        repo="sso-service",
    )
    assert issue.node_id == github_issue_id("demo", "demo-org/sso-service", 42)
    assert upsert_document(graph, issue).applied is True
    assert graph.query(
        "MATCH (i:Issue:Item {id: $id}) RETURN i.id",
        {"id": issue.node_id},
    ).result_set == [[issue.node_id]]


def test_sync_record_round_trip():
    store = MemoryStore()
    sync_id = "tenant1:slack:sync:T1:C0ENGSSO"
    write_channel_sync(
        store,
        sync_id=sync_id,
        fields={"tenant_id": "tenant1", "history_cursor": "page-2", "oldest": "0", "status": "in_progress"},
    )
    assert read_channel_sync(store, sync_id)["history_cursor"] == "page-2"
    mark_event_applied(store, "Ev1", "tenant1")
    assert event_already_applied(store, "Ev1") is True
    assert mark_event_applied(store, "Ev1", "tenant1") is None
    assert event_already_applied(store, "Ev1") is True


def test_connect_graph_refuses_a_missing_or_acme_tenant(monkeypatch):
    monkeypatch.delenv("WEAVE_TENANT", raising=False)
    with pytest.raises(ValueError):
        connect_graph()
    monkeypatch.setenv("WEAVE_TENANT", "acme")
    with pytest.raises(ValueError):
        connect_graph()


def test_postgres_ingest_record_round_trip():
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL is not set")
    from shared.pg import connect_pg

    sql = open("migrations/002_ingest_record.sql", encoding="utf-8").read()
    conn = connect_pg(url=dsn)
    record_id = "test:ingest:roundtrip"
    try:
        cur = conn.cursor()
        cur.execute(sql)
        conn.commit()
        store = PostgresStore(dsn)
        store.merge(record_id, "tenant1", "sync_state", {"status": "in_progress"})
        assert store.get(record_id)["payload"]["status"] == "in_progress"
        store.delete(record_id)
    finally:
        conn.close()
