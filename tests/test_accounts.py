"""Signup, login, and tenant isolation for the hosted pages."""

from fastapi.testclient import TestClient

from app.connect import accounts
from app.connect.credentials import slack_bot_token, use_store

accounts.SKIP_GRAPH = True
from app.connect.secrets import MemorySecretStore
from app.ingest.records import MemoryStore
from app.main import create_app


def _app(secrets=None):
    return create_app(tenant_id="real", store=MemoryStore(), secrets=secrets or MemorySecretStore())


def _signup(client, **fields):
    body = {"name": "Ada", "email": "ada@north.test", "password": "correct-horse", "company": "North"}
    body.update(fields)
    return client.post("/signup", data=body, follow_redirects=False)


def test_signup_creates_a_company_and_a_session():
    app = _app()
    client = TestClient(app)
    response = _signup(client)
    assert response.status_code == 303
    assert response.headers["location"].endswith("/setup")
    assert "weave_session" in response.cookies
    status = client.get("/setup/status").json()
    assert status["tenant"] == "north"
    assert status["connectors"][0]["id"] == "slack"
    use_store(None)


def test_acme_is_not_a_tenant():
    app = _app()
    client = TestClient(app)
    response = _signup(client, company="Acme")
    assert response.status_code == 303
    assert response.headers["location"].endswith("error=reserved")
    assert "weave_session" not in response.cookies
    use_store(None)


def test_wrong_password_does_not_start_a_session():
    app = _app()
    client = TestClient(app)
    _signup(client)
    response = client.post(
        "/login",
        data={"email": "ada@north.test", "password": "wrong-password"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert response.headers["location"].endswith("error=1")
    assert "weave_session" not in response.cookies
    use_store(None)


def test_a_second_company_does_not_see_the_first_grant(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxb-from-env")
    monkeypatch.setenv("SLACK_CLIENT_ID", "client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")
    secrets = MemorySecretStore()
    secrets.put("real", "slack", "TREAL", {"token": "xoxb-real"}, status="connected")
    app = _app(secrets)
    north = TestClient(app)
    _signup(north)
    status = north.get("/setup/status").json()
    assert status["tenant"] == "north"
    cards = {card["id"]: card for card in status["connectors"]}
    assert cards["slack"]["status"] == "disconnected"
    assert slack_bot_token("north") == ""

    class Conn:
        def cursor(self):
            return self

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def execute(self, sql, params=None):
            self.params = params

        def commit(self):
            return None

        def close(self):
            return None

    conn = Conn()
    app.state.pg_connect = lambda: conn
    secrets.put("north", "slack", "TNORTH", {"token": "xoxb-north"}, status="connected")
    opened = north.post("/setup/open")
    assert opened.status_code == 200
    assert conn.params[1] == "north"
    assert conn.params[2].startswith("north:person:")
    use_store(None)


def test_the_live_viewer_claims_tenant_real(monkeypatch):
    monkeypatch.setenv("WEAVE_VIEWER_EMAIL", "person@example.com")
    app = _app()
    client = TestClient(app)
    response = _signup(client, email="person@example.com", company="Something Else")
    assert response.status_code == 303
    assert client.get("/setup/status").json()["tenant"] == "real"
    use_store(None)


def test_slack_team_routes_to_its_tenant(monkeypatch):
    from app.main import _slack_event_tenant

    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    secrets = MemorySecretStore()
    secrets.put("north", "slack", "TNORTH", {"token": "xoxb-north"}, status="connected")
    app = _app(secrets)
    app.state.workspace_id = "TREAL"
    use_store(secrets)
    assert _slack_event_tenant(app, {"team_id": "TNORTH"}) == "north"
    assert _slack_event_tenant(app, {"team_id": "TREAL"}) == "real"
    # A workspace no company connected does not fall back to the env company.
    assert _slack_event_tenant(app, {"team_id": "TOTHER"}) is None
    assert _slack_event_tenant(app, {}) is None
    use_store(None)


class _Unique(Exception):
    sqlstate = "23505"

    def __init__(self, constraint):
        super().__init__(constraint)
        self.diag = type("Diag", (), {"constraint_name": constraint})()


class _RacedConn:
    """The checks find nothing, then the insert hits a unique index."""

    def __init__(self, constraint):
        self.constraint = constraint
        self.rolled_back = False

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=()):
        if sql.startswith("INSERT INTO tenant_membership"):
            raise _Unique(self.constraint)

    def fetchone(self):
        return None

    def commit(self):
        raise AssertionError("a raced signup must not commit")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        pass


def test_a_raced_signup_is_refused_by_the_unique_index(monkeypatch):
    import pytest

    for constraint, code in (("tenant_membership_one_owner", "taken"), ("app_user_email_key", "registered")):
        conn = _RacedConn(constraint)
        monkeypatch.setattr(accounts, "_connect", lambda: conn)
        monkeypatch.setattr(accounts, "hash_password", lambda secret: "hashed")
        with pytest.raises(accounts.AccountError) as caught:
            accounts.PostgresAccountStore().signup(
                email="ada@north.test", password="correct-horse", name="Ada", company="North",
            )
        assert caught.value.code == code
        assert conn.rolled_back


def test_one_owner_index_is_in_the_migrations():
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[1] / "migrations" / "011_one_owner.sql").read_text(encoding="utf-8")
    assert "UNIQUE INDEX IF NOT EXISTS tenant_membership_one_owner" in sql
    assert "WHERE role = 'owner'" in sql


class _TokenConn:
    """api_token as the routes see it. Records each statement and its params."""

    def __init__(self, rows=()):
        self.rows, self.calls, self.rowcount = list(rows), [], 1

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), tuple(params or ())))

    def fetchall(self):
        return self.rows

    def commit(self):
        return None

    def close(self):
        return None


def _signed_in_north(monkeypatch, conn):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    app = _app()
    app.state.pg_connect = lambda: conn
    client = TestClient(app)
    _signup(client)
    return app, client


def test_minted_tokens_expire_in_30_days_and_say_what_made_them(monkeypatch):
    conn = _TokenConn()
    app, client = _signed_in_north(monkeypatch, conn)
    app.state.secrets.put("north", "slack", "TNORTH", {"token": "xoxb-north"}, status="connected")

    assert client.post("/setup/open").status_code == 200
    assert client.post("/setup/install-code").status_code == 200

    inserts = [(sql, params) for sql, params in conn.calls if sql.startswith("INSERT INTO api_token")]
    assert [params[3] for _, params in inserts] == ["mcp", "pet"]
    assert all("now() + interval '30 days'" in sql for sql, _ in inserts)
    use_store(None)


def test_the_token_list_shows_only_a_short_id(monkeypatch):
    from datetime import UTC, datetime

    made = datetime(2026, 10, 28, tzinfo=UTC)
    conn = _TokenConn(rows=[("ab12cd34", "pet", made, made, None)])
    _, client = _signed_in_north(monkeypatch, conn)

    listed = client.get("/setup/tokens").json()
    assert listed["days"] == 30
    assert listed["tokens"] == [{"id": "ab12cd34", "label": "pet", "created": made.isoformat(),
                                 "expires": made.isoformat(), "revoked": None}]
    sql, params = conn.calls[-1]
    assert "left(token_sha256, 8)" in sql and "SELECT token_sha256" not in sql
    assert params == ("north", "north:person:ada@north.test")
    use_store(None)


def test_revoke_touches_only_the_callers_own_tokens(monkeypatch):
    conn = _TokenConn()
    _, client = _signed_in_north(monkeypatch, conn)

    assert client.post("/setup/tokens/revoke", json={"id": "ab12cd34"}).json() == {"revoked": 1}
    sql, params = conn.calls[-1]
    assert sql.startswith("UPDATE api_token SET revoked_at = now() WHERE tenant_id = %s AND person_id = %s")
    assert params == ("north", "north:person:ada@north.test", "ab12cd34")

    assert client.post("/setup/tokens/revoke", json={"all": True}).status_code == 200
    assert conn.calls[-1][1] == ("north", "north:person:ada@north.test")

    assert client.post("/setup/tokens/revoke", json={"id": "' OR 1=1 --"}).status_code == 400
    assert client.post("/setup/tokens/revoke", json={}).status_code == 400
    use_store(None)


def test_token_routes_need_a_session():
    client = TestClient(_app())
    assert client.get("/setup/tokens").status_code == 401
    assert client.post("/setup/tokens/revoke", json={"all": True}).status_code == 401
    use_store(None)


def test_expired_tokens_are_refused_everywhere():
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    migration = (root / "migrations" / "012_token_expiry.sql").read_text(encoding="utf-8")
    assert "ADD COLUMN IF NOT EXISTS expires_at" in migration
    assert "created_at + interval '30 days'" in migration
    function = migration.split("CREATE OR REPLACE FUNCTION weave_token_owner", 1)[1]
    assert "t.revoked_at IS NULL" in function
    assert "t.expires_at IS NULL OR t.expires_at > now()" in function
    main = (root / "app" / "main.py").read_text(encoding="utf-8")
    assert "expires_at IS NULL OR expires_at > now()" in main
