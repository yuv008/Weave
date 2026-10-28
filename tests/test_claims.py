from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.graph.upsert import upsert_message
from app.ingest.records import MemoryStore
from app.main import create_app
from app.models.canonical import CanonicalMessage
from app.slack.normalize import format_slack_version

TENANT = "tenant1"
AUTH = {"authorization": "Bearer viewer"}


def _allow(token: str) -> str | None:
    return TENANT if token == "viewer" else None


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
    app = create_app(graph=graph, tenant_id=TENANT, store=store, viewer_auth=_allow)
    response = TestClient(app).post(
        "/claims/commitment",
        headers=AUTH,
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
    app = create_app(graph=graph, tenant_id=TENANT, store=MemoryStore(), viewer_auth=_allow)
    client = TestClient(app)
    body = {
        "item_id": message.node_id,
        "statement": "Ship SSO",
        "quote": "SSO work",
        "due": "2026-10-20",
        "progress": "open",
    }
    first = client.post("/claims/commitment", headers=AUTH, json=body)
    second = client.post("/claims/commitment", headers=AUTH, json=body)
    assert first.status_code == 200
    assert second.json()["id"] == first.json()["id"]
    assert graph.query("MATCH (c:Commitment) RETURN count(c)").result_set[0][0] == 1


def test_supersede_writes_one_event_and_retires_the_old_claim(graph):
    message = _message("The budget is 12 percent")
    upsert_message(graph, message)
    store = MemoryStore()
    app = create_app(graph=graph, tenant_id=TENANT, store=store, viewer_auth=_allow)
    client = TestClient(app)
    created = client.post(
        "/claims/figure",
        headers=AUTH,
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
        headers=AUTH,
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
        headers=AUTH,
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


def test_claim_without_a_token_is_rejected(graph):
    app = create_app(graph=graph, tenant_id=TENANT, store=MemoryStore())
    response = TestClient(app).post("/claims/commitment", json={"item_id": "x"})
    assert response.status_code == 401
    response = TestClient(app).post(
        "/slack/decisions",
        json={"channel_id": "C1", "statement": "Ship", "quote": "Ship"},
    )
    assert response.status_code == 401


def test_a_claim_uses_the_token_company_not_the_env_company(monkeypatch):
    opened, seen = [], []

    class Empty:
        def query(self, query, params=None):
            seen.append(dict(params or {}))
            return type("Result", (), {"result_set": []})()

    def connect_graph(tenant=None):
        opened.append(tenant)
        return Empty()

    monkeypatch.setattr("app.graph.client.connect_graph", connect_graph)
    app = create_app(
        tenant_id="real", store=MemoryStore(),
        viewer_auth=lambda token: "north" if token == "north-token" else None,
    )
    client = TestClient(app)
    body = {"item_id": "real:slack:message:C1:20", "statement": "Ship SSO", "quote": "Ship",
            "due": "2026-10-20", "progress": "open"}

    response = client.post("/claims/commitment", headers={"authorization": "Bearer north-token"}, json=body)
    decisions = client.post(
        "/slack/decisions", headers={"authorization": "Bearer north-token"},
        json={"channel_id": "C1", "statement": "Use SSO", "quote": "Use SSO"},
    )

    assert response.status_code == 400
    assert decisions.status_code == 200
    assert opened == ["north", "north"]
    assert seen and all(params.get("tenant_id", "north") == "north" for params in seen)
    unknown = client.post("/claims/commitment", headers={"authorization": "Bearer other"}, json=body)
    assert unknown.status_code == 401
