"""Hosted connect page: auth refusal, OAuth state, stored tokens, and connector cards."""

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.connect.credentials import slack_bot_token, use_store
from app.connect.oauth import complete
from app.connect.secrets import MemorySecretStore
from app.ingest.records import MemoryStore
from app.main import _run_slack_reply, create_app


def _app(secrets=None):
    return create_app(tenant_id="real", store=MemoryStore(), secrets=secrets or MemorySecretStore())


def test_oauth_state_mismatch_is_rejected():
    app = _app()
    app.state.oauth_states.put("abc", "real", "notion")
    response = TestClient(app).get("/connect/slack/callback?state=abc&code=nope", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["location"].endswith("error=state")


def test_stored_slack_token_is_what_the_reply_path_uses(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
    secrets = MemorySecretStore()
    secrets.put("real", "slack", "T1", {"token": "xoxb-from-store"})
    seen = {}

    class Api:
        def __init__(self, token):
            seen["token"] = token

    def answer_event(*args, **kwargs):
        seen["slack"] = kwargs["slack"]

    monkeypatch.setattr("agents.slack.client.SlackApi", Api)
    monkeypatch.setattr("agents.answer.agent.build_agent", lambda runtime: None)
    monkeypatch.setattr("agents.runtime.live_runtime", lambda: SimpleNamespace(cfg=SimpleNamespace(tenant="real")))
    monkeypatch.setattr("agents.slack.reply.answer_event", answer_event)
    app = _app(secrets)
    _run_slack_reply(app, {}, object())
    assert seen["token"] == "xoxb-from-store"
    assert slack_bot_token("nobody") == "xoxb-from-env"
    use_store(None)


def test_callback_saves_the_slack_token_from_the_exchange(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    monkeypatch.setenv("SLACK_CLIENT_ID", "client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")
    secrets = MemorySecretStore()
    app = _app(secrets)
    app.state.oauth_states.put("good", "real", "slack")

    def http(url, fields, headers=None):
        assert "oauth.v2.access" in url
        return {"ok": True, "access_token": "xoxb-callback", "team": {"id": "T9"}}

    complete(
        "slack",
        "good",
        {"code": "code"},
        app.state.oauth_states,
        secrets,
        redirect_uri="http://test/connect/slack/callback",
        http=http,
    )
    assert slack_bot_token("real") == "xoxb-callback"
    use_store(None)


def test_connector_cards_include_the_four_tools_and_leave_hubspot_closed():
    app = _app()
    page = TestClient(app).get("/")
    assert page.status_code == 200
    assert 'data-provider="slack"' in page.text
    assert 'data-provider="notion"' in page.text
    assert 'data-provider="google"' in page.text
    assert 'data-provider="github"' in page.text
    assert 'data-provider="hubspot" data-connectable="false"' in page.text
    status = TestClient(app).get("/setup/status").json()
    cards = {card["id"]: card for card in status["connectors"]}
    assert cards["hubspot"]["connectable"] is False
    assert cards["hubspot"]["status"] == "unavailable"


def test_open_weave_requires_a_connection_and_then_mints_a_token(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    monkeypatch.setenv("WEAVE_VIEWER_EMAIL", "person@example.com")
    secrets = MemorySecretStore()
    app = _app(secrets)
    client = TestClient(app)
    blocked = client.post("/setup/open")
    assert blocked.status_code == 400
    secrets.put("real", "slack", "T1", {"token": "xoxb-ready"}, status="connected")
    saved = {}

    class Conn:
        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            saved["sql"] = sql
            saved["params"] = params

        def commit(self):
            saved["committed"] = True

        def close(self):
            saved["closed"] = True

    app.state.pg_connect = lambda: Conn()
    opened = client.post("/setup/open")
    assert opened.status_code == 200
    body = opened.json()
    assert body["token"].startswith("wv_")
    assert body["pet"]["cue_url"].endswith("/wisp/cue")
    assert body["pet"]["token"] == body["token"]
    assert body["token"] not in json.dumps(client.get("/setup/status").json())
    assert saved["committed"] is True
    use_store(None)
