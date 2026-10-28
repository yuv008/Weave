"""Hosted connect page: auth refusal, OAuth state, stored tokens, and connector cards."""

import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.connect import accounts
from app.connect.credentials import slack_bot_token, use_store

accounts.SKIP_GRAPH = True
from app.connect.oauth import complete
from app.connect.routes import _schedule
from app.connect.secrets import MemorySecretStore
from app.ingest.records import MemoryStore
from app.main import _run_slack_reply, create_app


def _app(secrets=None):
    return create_app(tenant_id="real", store=MemoryStore(), secrets=secrets or MemorySecretStore())


def _signed_in(app, email="owner@north.test", company="North", name="Owner"):
    client = TestClient(app)
    response = client.post(
        "/signup",
        data={"name": name, "email": email, "password": "correct-horse", "company": company},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("/setup")
    return client


def test_oauth_state_mismatch_is_rejected(monkeypatch):
    monkeypatch.setenv("WEAVE_VIEWER_EMAIL", "person@example.com")
    app = _app()
    client = _signed_in(app, email="person@example.com", company="Weave")
    app.state.oauth_states.put("abc", "real", "notion")
    response = client.get("/connect/slack/callback?state=abc&code=nope", follow_redirects=False)
    assert response.status_code == 303
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
    assert slack_bot_token("nobody") == ""
    assert slack_bot_token() == "xoxb-from-env"
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


def test_landing_page_links_to_setup_and_uses_the_mark():
    app = _app()
    page = TestClient(app).get("/")
    assert page.status_code == 200
    assert "Connected knowledge" in page.text
    assert 'href="/enter"' in page.text
    enter = TestClient(app).get("/enter", follow_redirects=False)
    assert enter.status_code == 303
    assert enter.headers["location"].endswith("/signup")
    assert "/brand/mark.png" in page.text
    assert "/landing/stage.js" in page.text
    stage = TestClient(app).get("/landing/stage.js")
    assert stage.status_code == 200
    assert stage.headers["content-type"].startswith("text/javascript")
    assert "SUPERSEDES" in stage.text
    blocked = TestClient(app).get("/landing/secret.env")
    assert blocked.status_code == 404
    mark = TestClient(app).get("/brand/mark.png")
    assert mark.status_code == 200
    assert mark.headers["content-type"].startswith("image/")
    missing = TestClient(app).get("/brand/secret.env")
    assert missing.status_code == 404


def test_connector_cards_include_the_four_tools_and_leave_hubspot_closed():
    app = _app()
    client = _signed_in(app)
    page = client.get("/setup")
    assert page.status_code == 200
    assert 'data-provider="slack"' in page.text
    assert 'data-provider="notion"' in page.text
    assert 'data-provider="google"' in page.text
    assert 'data-provider="github"' in page.text
    assert 'data-provider="hubspot" data-connectable="false"' in page.text
    status = client.get("/setup/status").json()
    cards = {card["id"]: card for card in status["connectors"]}
    assert cards["hubspot"]["connectable"] is False
    assert cards["hubspot"]["status"] == "unavailable"


def test_open_weave_requires_a_connection_and_then_mints_a_token(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    monkeypatch.setenv("WEAVE_VIEWER_EMAIL", "person@example.com")
    secrets = MemorySecretStore()
    app = _app(secrets)
    client = _signed_in(app, email="person@example.com", company="Weave")
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


class _Now:
    def add_task(self, fn):
        fn()


def test_missing_embedder_marks_the_grant_connected(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    secrets = MemorySecretStore()
    secrets.put("real", "notion", "workspace", {"token": "ntn"}, status="syncing")
    app = _app(secrets)

    def boom(provider, tenant):
        raise ModuleNotFoundError("sentence_transformers")

    app.state.poll_now = boom
    _schedule(app, _Now(), "notion")
    assert secrets.get("real", "notion")["status"] == "connected"
    use_store(None)


def test_a_connector_error_marks_the_grant_failed(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    secrets = MemorySecretStore()
    secrets.put("real", "notion", "workspace", {"token": "ntn"}, status="syncing")
    app = _app(secrets)

    def boom(provider, tenant):
        raise RuntimeError("notion")

    app.state.poll_now = boom
    _schedule(app, _Now(), "notion")
    assert secrets.get("real", "notion")["status"] == "failed"
    use_store(None)


def _github_env(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    monkeypatch.setenv("GITHUB_APP_SLUG", "weave-test")
    monkeypatch.setenv("GITHUB_APP_CLIENT_ID", "client")
    monkeypatch.setenv("GITHUB_APP_CLIENT_SECRET", "secret")


def _github_http(reachable):
    """GitHub as seen by a user who can reach the installs in ``reachable``."""

    def http(url, fields, headers=None):
        if url == "https://github.com/login/oauth/access_token":
            assert fields["code"] == "user-code"
            return {"access_token": "ghu_user"}
        assert url.startswith("https://api.github.com/user/installations")
        assert headers["Authorization"] == "Bearer ghu_user"
        return {"total_count": len(reachable), "installations": [{"id": int(i)} for i in reachable]}

    return http


def _github_callback(app, secrets, query, http, tenant="north"):
    app.state.oauth_states.put("gh-state", tenant, "github")
    return complete("github", "gh-state", query, app.state.oauth_states, secrets,
                    redirect_uri="http://test/connect/github/callback", http=http)


def test_github_install_the_user_can_reach_is_saved(monkeypatch):
    _github_env(monkeypatch)
    secrets = MemorySecretStore()
    app = _app(secrets)
    http = _github_http({"111"})
    _github_callback(app, secrets, {"installation_id": "111", "code": "user-code"}, http)
    assert secrets.get("north", "github")["installation_id"] == "111"
    use_store(None)


def test_a_forged_github_install_id_is_refused(monkeypatch):
    import pytest

    _github_env(monkeypatch)
    secrets = MemorySecretStore()
    app = _app(secrets)
    with pytest.raises(ValueError):
        _github_callback(app, secrets, {"installation_id": "999", "code": "user-code"}, _github_http({"111"}))
    with pytest.raises(ValueError):
        _github_callback(app, secrets, {"installation_id": "111"}, _github_http({"111"}))
    assert secrets.get("north", "github") is None
    use_store(None)


def test_a_github_install_held_by_another_company_is_refused(monkeypatch):
    import pytest

    _github_env(monkeypatch)
    secrets = MemorySecretStore()
    secrets.put("real", "github", "111", {"installation_id": "111"}, status="connected")
    app = _app(secrets)
    with pytest.raises(ValueError):
        _github_callback(app, secrets, {"installation_id": "111", "code": "user-code"}, _github_http({"111"}))
    assert secrets.get("north", "github") is None
    use_store(None)


def test_github_connect_needs_the_app_client_secret(monkeypatch):
    from app.connect.oauth import configured

    monkeypatch.setenv("GITHUB_APP_SLUG", "weave-test")
    monkeypatch.delenv("GITHUB_APP_CLIENT_ID", raising=False)
    monkeypatch.delenv("GITHUB_APP_CLIENT_SECRET", raising=False)
    assert configured("github")[0] is False
    _github_env(monkeypatch)
    assert configured("github") == (True, "")
