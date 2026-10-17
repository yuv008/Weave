from datetime import UTC, datetime

import pytest
from fastapi.testclient import TestClient

from app.answer.decision import channel_decisions
from app.graph.upsert import upsert_message
from app.main import create_app
from app.models.canonical import EXCERPT_LIMIT, CanonicalMessage
from app.slack.normalize import format_slack_version

TENANT = "tenant1"
CHANNEL = "C0ENGSSO"
STATEMENT = "Ship SSO with provider A"
QUOTE = "provider A"


def _message(excerpt: str, ts: str, *, deleted: bool = False) -> CanonicalMessage:
    return CanonicalMessage(
        tenant_id=TENANT,
        connection_id="conn1",
        workspace_id="T1",
        external_id=f"{CHANNEL}:{ts}",
        conversation_external_id=CHANNEL,
        author_external_id="U1",
        occurred_at=datetime.fromtimestamp(int(ts), tz=UTC),
        version=format_slack_version(ts),
        excerpt="" if deleted else excerpt,
        content_hash="ab" * 32,
        source_url="" if deleted else f"https://acme.slack.com/archives/{CHANNEL}/p{ts}",
        deleted=deleted,
    )


def _count(graph, label: str) -> int:
    return graph.query(f"MATCH (n:{label}) RETURN count(n)").result_set[0][0]


class _Recording:
    def query(self, query, params=None):
        raise AssertionError(query)


def test_blank_or_oversized_quote_does_not_query():
    with pytest.raises(ValueError, match="quote is required"):
        channel_decisions(_Recording(), TENANT, CHANNEL, STATEMENT, "  ")
    with pytest.raises(ValueError, match="longer than an excerpt"):
        channel_decisions(_Recording(), TENANT, CHANNEL, STATEMENT, "q" * (EXCERPT_LIMIT + 1))


def test_live_matches_skip_deleted_and_placeholder_and_write_nothing(graph):
    newer = _message("ship with provider A now", "30")
    older = _message("provider A was the choice", "20")
    deleted = _message("provider A secret", "10", deleted=True)
    placeholder = _message("provider A placeholder", "5")
    for message in (newer, older, deleted, placeholder):
        upsert_message(graph, message)
    graph.query(
        "MATCH (m:Message {id: $id}) SET m.excerpt = $excerpt, m.deleted = true",
        {"id": deleted.node_id, "excerpt": "provider A secret"},
    )
    graph.query(
        "MATCH (m:Message {id: $id}) SET m.placeholder = true",
        {"id": placeholder.node_id},
    )
    before = _count(graph, "Message")

    decisions = channel_decisions(graph, TENANT, CHANNEL, STATEMENT, QUOTE)

    assert _count(graph, "Message") == before
    assert _count(graph, "Decision") == 2
    channel_decisions(graph, TENANT, CHANNEL, STATEMENT, QUOTE)
    assert _count(graph, "Decision") == 2
    assert [item.evidence.message_id for item in decisions] == [newer.node_id, older.node_id]
    assert [item.statement for item in decisions] == [STATEMENT, STATEMENT]
    assert [item.status for item in decisions] == ["active", "active"]
    assert decisions[0].evidence.quote == QUOTE
    assert decisions[0].evidence.source_url == newer.source_url
    assert "provider A secret" not in [item.evidence.quote for item in decisions]
    assert placeholder.node_id not in [item.evidence.message_id for item in decisions]


def test_missing_quote_returns_no_decisions(graph):
    upsert_message(graph, _message("something else", "20"))

    assert channel_decisions(graph, TENANT, CHANNEL, STATEMENT, QUOTE) == []


def test_twenty_first_match_is_absent(graph):
    for second in range(1, 22):
        upsert_message(graph, _message(f"provider A at {second}", str(second)))

    decisions = channel_decisions(graph, TENANT, CHANNEL, STATEMENT, QUOTE)

    ids = [item.evidence.message_id for item in decisions]
    assert len(ids) == 20
    assert ids[0] == _message("provider A at 21", "21").node_id
    assert _message("provider A at 1", "1").node_id not in ids


def _allow(token: str) -> bool:
    return token == "viewer"


def _client(graph):
    return TestClient(create_app(graph=graph, tenant_id=TENANT, viewer_auth=_allow))


def _post(client, path, payload):
    return client.post(path, headers={"authorization": "Bearer viewer"}, json=payload)


def test_blank_quote_route_returns_400_without_a_query():
    response = _post(
        _client(_Recording()),
        "/slack/decisions",
        {"channel_id": CHANNEL, "statement": STATEMENT, "quote": " "},
    )

    assert response.status_code == 400


def test_route_returns_the_decision_json(graph):
    message = _message("ship with provider A", "30")
    upsert_message(graph, message)

    response = _post(
        _client(graph),
        "/slack/decisions",
        {"channel_id": CHANNEL, "statement": STATEMENT, "quote": QUOTE},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["decisions"][0]["statement"] == STATEMENT
    assert body["decisions"][0]["status"] == "active"
    assert body["decisions"][0]["evidence"]["quote"] == QUOTE
    assert body["decisions"][0]["evidence"]["source_url"] == message.source_url
    assert body["decisions"][0]["evidence"]["message_id"] == message.node_id
    assert _count(graph, "Decision") == 1
    again = _post(
        _client(graph),
        "/slack/decisions",
        {"channel_id": CHANNEL, "statement": STATEMENT, "quote": QUOTE},
    )
    assert again.status_code == 200
    assert _count(graph, "Decision") == 1
