import hashlib
import hmac
import json
import time
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from app.graph.upsert import upsert_message
from app.main import create_app
from app.models.canonical import CanonicalMessage
from app.slack.normalize import format_slack_version

SECRET = "signing-secret"
TENANT = "tenant1"
CONNECTION = "conn1"
WORKSPACE = "T1"
CHANNEL = "C0ENGSSO"


def _sign(body: bytes, timestamp: str) -> str:
    digest = hmac.new(SECRET.encode(), f"v0:{timestamp}:".encode() + body, hashlib.sha256).hexdigest()
    return f"v0={digest}"


def _post(client: TestClient, payload: dict, *, signature: str | None = None):
    if payload.get("type") == "event_callback":
        # Slack puts the workspace on every event callback.
        payload = {"team_id": WORKSPACE, **payload}
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    return client.post(
        "/slack/events",
        content=body,
        headers={
            "content-type": "application/json",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": signature if signature is not None else _sign(body, timestamp),
        },
    )


def _app(graph):
    return TestClient(
        create_app(
            graph=graph,
            signing_secret=SECRET,
            tenant_id=TENANT,
            connection_id=CONNECTION,
            workspace_id=WORKSPACE,
            workspace_domain="acme.slack.com",
        )
    )


def _seed(graph, excerpt: str, version_ts: str):
    message = CanonicalMessage(
        tenant_id=TENANT,
        connection_id=CONNECTION,
        workspace_id=WORKSPACE,
        external_id=f"{CHANNEL}:10.1",
        conversation_external_id=CHANNEL,
        author_external_id="U1",
        occurred_at=datetime(2014, 6, 18, tzinfo=UTC),
        version=format_slack_version(version_ts),
        excerpt=excerpt,
        content_hash="a" * 64,
        source_url=f"https://acme.slack.com/archives/{CHANNEL}/p101",
    )
    upsert_message(graph, message)


def _excerpt(graph) -> str:
    return graph.query(
        "MATCH (m:Message {id: $id}) RETURN m.excerpt",
        {"id": f"{TENANT}:slack:message:{CHANNEL}:10.1"},
    ).result_set[0][0]


def test_bad_signature_does_not_touch_the_graph():
    class Recording:
        def __init__(self):
            self.calls = 0

        def query(self, query, params=None):
            self.calls += 1
            raise AssertionError(query)

    graph = Recording()
    response = _post(_app(graph), {"type": "url_verification", "challenge": "abc"}, signature="v0=nope")

    assert response.status_code == 401
    assert graph.calls == 0


def test_url_verification_returns_the_challenge():
    response = _post(_app(_Silent()), {"type": "url_verification", "challenge": "handshake"})

    assert response.status_code == 200
    assert response.json()["challenge"] == "handshake"


def test_delete_blanks_the_excerpt_and_keeps_contains(graph):
    _seed(graph, "keep the edge", "10.1")
    response = _post(
        _app(graph),
        {
            "type": "event_callback",
            "event_id": "Ev-delete",
            "event": {
                "type": "message",
                "subtype": "message_deleted",
                "channel": CHANNEL,
                "channel_type": "channel",
                "ts": "10.4",
                "deleted_ts": "10.1",
                "previous_message": {"user": "U1", "text": "keep the edge", "ts": "10.1"},
            },
        },
    )

    assert response.status_code == 200
    assert _excerpt(graph) == ""
    in_source = graph.query(
        """
        MATCH (:Message {id: $id})-[edge:IN_SOURCE]->(:Conversation)
        RETURN count(edge)
        """,
        {"id": f"{TENANT}:slack:message:{CHANNEL}:10.1"},
    ).result_set
    assert in_source[0][0] == 1


def test_older_edit_does_not_replace_a_newer_excerpt(graph):
    _seed(graph, "current wording", "30.0")
    response = _post(
        _app(graph),
        {
            "type": "event_callback",
            "event_id": "Ev-old",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": CHANNEL,
                "channel_type": "channel",
                "message": {
                    "user": "U1",
                    "text": "stale wording",
                    "ts": "10.1",
                    "edited": {"user": "U1", "ts": "20.0"},
                },
            },
        },
    )

    assert response.status_code == 200
    assert _excerpt(graph) == "current wording"


def test_redelivered_event_does_not_apply_a_newer_body(graph):
    payload = {
        "type": "event_callback",
        "event_id": "Ev-same",
        "event": {
            "type": "message",
            "channel": CHANNEL,
            "channel_type": "channel",
            "user": "U1",
            "text": "alpha",
            "ts": "10.1",
        },
    }
    client = _app(graph)
    assert _post(client, payload).status_code == 200
    payload["event"] = {**payload["event"], "text": "beta", "edited": {"user": "U1", "ts": "40.0"}}
    payload["event"]["subtype"] = "message_changed"
    payload["event"]["message"] = {
        "user": "U1",
        "text": "beta",
        "ts": "10.1",
        "edited": {"user": "U1", "ts": "40.0"},
    }
    assert _post(client, payload).status_code == 200
    assert _excerpt(graph) == "alpha"


class _Silent:
    def query(self, query, params=None):
        return type("Result", (), {"result_set": []})()


class _Untouched:
    def query(self, query, params=None):
        raise AssertionError(query)


def _message_from(team: str) -> dict:
    return {
        "type": "event_callback",
        "team_id": team,
        "event_id": f"Ev-{team}",
        "event": {"type": "message", "channel": CHANNEL, "channel_type": "channel", "user": "U9", "text": "hi", "ts": "50.1"},
    }


def test_an_event_from_a_workspace_no_company_owns_is_dropped():
    response = _post(_app(_Untouched()), _message_from("TSTRANGER"))

    assert response.status_code == 200
    assert response.json() == {"ok": True}


def test_a_failed_workspace_lookup_asks_slack_to_retry():
    from app.connect.credentials import use_store

    class Broken:
        def find_tenant(self, provider, external_id):
            raise ConnectionError("postgres is down")

    client = _app(_Untouched())
    use_store(Broken())
    try:
        response = _post(client, _message_from("TSTRANGER"))
    finally:
        use_store(None)

    assert response.status_code == 503
