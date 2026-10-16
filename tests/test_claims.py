from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.graph.upsert import upsert_message
from app.ingest.records import MemoryStore
from app.main import create_app
from app.models.canonical import CanonicalMessage
from app.slack.normalize import format_slack_version

TENANT = "tenant1"


def _message(excerpt: str) -> CanonicalMessage:
    return CanonicalMessage(
        tenant_id=TENANT,
        connection_id="conn1",
        workspace_id="T1",
        external_id="C0ENGSSO:20",
        conversation_external_id="C0ENGSSO",
        author_external_id="U1",
        occurred_at=datetime.fromtimestamp(20, tz=UTC),
        version=format_slack_version("20"),
        excerpt=excerpt,
        content_hash="ab" * 32,
        source_url="https://acme.slack.com/archives/C0ENGSSO/p20",
        deleted=False,
    )


def test_missing_quote_is_rejected(graph):
    upsert_message(graph, _message("Ship the SSO work by Friday"))
    store = MemoryStore()
    app = create_app(graph=graph, tenant_id=TENANT, store=store)
    response = TestClient(app).post(
        "/claims/commitment",
        json={
            "item_id": "tenant1:slack:message:C0ENGSSO:20",
            "statement": "Ship SSO",
            "quote": "not in the message",
            "due": "2026-10-20",
            "progress": "open",
        },
    )
    assert response.status_code == 400
    assert store.changes == []


def test_repeat_commitment_merges(graph):
    message = _message("Ship the SSO work by Friday")
    upsert_message(graph, message)
    app = create_app(graph=graph, tenant_id=TENANT, store=MemoryStore())
    client = TestClient(app)
    body = {
        "item_id": message.node_id,
        "statement": "Ship SSO",
        "quote": "SSO work",
        "due": "2026-10-20",
        "progress": "open",
    }
    first = client.post("/claims/commitment", json=body)
    second = client.post("/claims/commitment", json=body)
    assert first.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert graph.query("MATCH (c:Commitment) RETURN count(c)").result_set[0][0] == 1


def test_supersede_writes_one_event_and_retires_the_old_claim(graph):
    message = _message("The budget is 12 percent")
    upsert_message(graph, message)
    store = MemoryStore()
    app = create_app(graph=graph, tenant_id=TENANT, store=store)
    client = TestClient(app)
    created = client.post(
        "/claims/figure",
        json={
            "item_id": message.node_id,
            "name": "budget",
            "value": 12,
            "unit": "percent",
            "as_of": "2026-10-01",
            "quote": "12 percent",
        },
    )
    replaced = client.post(
        "/claims/supersede",
        json={
            "old_id": created.json()["id"],
            "kind": "figure",
            "item_id": message.node_id,
            "name": "budget",
            "value": 9,
            "unit": "percent",
            "as_of": "2026-10-16",
            "quote": "12 percent",
        },
    )
    again = client.post(
        "/claims/supersede",
        json={
            "old_id": created.json()["id"],
            "kind": "figure",
            "item_id": message.node_id,
            "name": "budget",
            "value": 8,
            "unit": "percent",
            "as_of": "2026-10-16",
            "quote": "12 percent",
        },
    )
    assert replaced.status_code == 200
    assert again.status_code == 400
    assert len(store.changes) == 1
    assert store.changes[0]["id"] == f"{created.json()['id']}:superseded"
    status = graph.query(
        "MATCH (c:Figure {id: $id}) RETURN c.status",
        {"id": created.json()["id"]},
    ).result_set[0][0]
    assert status == "superseded"
