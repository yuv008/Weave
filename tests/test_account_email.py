"""Email verification, logout, and password reset on the hosted pages."""

import re
import urllib.parse

from fastapi.testclient import TestClient

from app.connect import accounts
from app.connect.accounts import MemoryAccountStore
from app.connect.credentials import use_store
from app.connect.mailer import MemoryMailer
from app.connect.secrets import MemorySecretStore
from app.ingest.records import MemoryStore
from app.main import create_app

accounts.SKIP_GRAPH = True
SIGNUP = {"name": "Ada", "email": "ada@north.test", "password": "correct-horse", "company": "North"}


def _app(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    monkeypatch.setenv("WEAVE_PUBLIC_URL", "https://weave.test")
    mailer = MemoryMailer()
    app = create_app(tenant_id="real", store=MemoryStore(), secrets=MemorySecretStore(),
                     accounts=MemoryAccountStore(auto_verify=False), mailer=mailer)
    return app, mailer


def _token(mail: dict, path: str) -> str:
    found = re.search(r"https://weave\.test" + re.escape(path) + r"\?token=(\S+)", mail["text"])
    assert found, mail["text"]
    return urllib.parse.unquote(found.group(1))


def test_signup_mails_a_link_and_the_link_verifies_once(monkeypatch):
    app, mailer = _app(monkeypatch)
    client = TestClient(app)
    client.post("/signup", data=SIGNUP)

    assert client.get("/session").json()["verified"] is False
    assert [mail["to"] for mail in mailer.sent] == ["ada@north.test"]
    token = _token(mailer.sent[0], "/verify")

    first = client.get(f"/verify?token={urllib.parse.quote(token)}", follow_redirects=False)
    assert first.headers["location"] == "/setup?verified=1"
    assert client.get("/session").json()["verified"] is True
    again = client.get(f"/verify?token={urllib.parse.quote(token)}", follow_redirects=False)
    assert again.headers["location"] == "/setup?verified=expired"
    use_store(None)


def test_an_unverified_account_cannot_connect_or_mint(monkeypatch):
    monkeypatch.setenv("SLACK_CLIENT_ID", "client")
    monkeypatch.setenv("SLACK_CLIENT_SECRET", "secret")
    app, _mailer = _app(monkeypatch)
    client = TestClient(app)
    client.post("/signup", data=SIGNUP)

    for path, body in (("/setup/open", {}), ("/setup/install-code", {}), ("/setup/model", {"key": "gsk"}),
                       ("/setup/google/folder", {"folder_id": "f"}), ("/setup/github/repo", {"owner": "o", "repo": "r"})):
        response = client.post(path, json=body)
        assert response.status_code == 403 and response.json()["needs"] == "verify", path
    start = client.get("/connect/slack", follow_redirects=False)
    assert start.headers["location"] == "/setup?error=verify"
    # Seeing the page and the company is still allowed.
    assert client.get("/setup/status").status_code == 200
    use_store(None)


def test_resend_mails_a_new_link_only_while_unverified(monkeypatch):
    app, mailer = _app(monkeypatch)
    client = TestClient(app)
    client.post("/signup", data=SIGNUP)

    assert client.post("/verify/resend").json() == {"sent": True}
    assert len(mailer.sent) == 2
    client.get(f"/verify?token={urllib.parse.quote(_token(mailer.sent[1], '/verify'))}")
    assert client.post("/verify/resend").json() == {"verified": True}
    assert len(mailer.sent) == 2
    use_store(None)


def test_logout_ends_the_session(monkeypatch):
    app, _mailer = _app(monkeypatch)
    client = TestClient(app)
    client.post("/signup", data=SIGNUP)
    raw = client.cookies.get("weave_session")

    response = client.post("/logout", follow_redirects=False)
    assert response.headers["location"] == "/login"
    assert client.get("/setup/status").status_code == 401
    assert app.state.accounts.read_session(raw) is None
    use_store(None)


def test_forgot_says_the_same_for_any_address_and_mails_only_real_ones(monkeypatch):
    app, mailer = _app(monkeypatch)
    TestClient(app).post("/signup", data=SIGNUP)
    mailer.sent.clear()
    client = TestClient(app)

    unknown = client.post("/forgot", data={"email": "nobody@north.test"}, follow_redirects=False)
    known = client.post("/forgot", data={"email": "ada@north.test"}, follow_redirects=False)
    assert unknown.headers["location"] == known.headers["location"] == "/forgot?sent=1"
    assert [mail["to"] for mail in mailer.sent] == ["ada@north.test"]
    use_store(None)


def test_reset_changes_the_password_once_and_ends_every_session(monkeypatch):
    app, mailer = _app(monkeypatch)
    signed_in = TestClient(app)
    signed_in.post("/signup", data=SIGNUP)
    client = TestClient(app)
    client.post("/forgot", data={"email": "ada@north.test"})
    token = _token(mailer.sent[-1], "/reset")

    weak = client.post("/reset", data={"token": token, "password": "short"}, follow_redirects=False)
    assert "error=password" in weak.headers["location"]
    done = client.post("/reset", data={"token": token, "password": "a-new-long-password"}, follow_redirects=False)
    assert done.headers["location"] == "/login?reset=1"

    assert signed_in.get("/setup/status").status_code == 401
    assert app.state.accounts.login("ada@north.test", "correct-horse") is None
    _raw, row = app.state.accounts.login("ada@north.test", "a-new-long-password")
    # The link came by email, so the address now counts as verified.
    assert row["verified"] is True
    reused = client.post("/reset", data={"token": token, "password": "another-long-password"}, follow_redirects=False)
    assert reused.headers["location"] == "/reset?error=expired"
    use_store(None)


def test_the_mailer_strips_the_spaces_google_shows_in_app_passwords(monkeypatch):
    from app.connect.mailer import SmtpMailer, mailer_from_env

    monkeypatch.delenv("SMTP_USER", raising=False)
    assert mailer_from_env() is None
    monkeypatch.setenv("SMTP_USER", "weave@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "abcd efgh ijkl mnop")
    mailer = mailer_from_env()
    assert isinstance(mailer, SmtpMailer)
    assert mailer.password == "abcdefghijklmnop" and mailer.sender == "weave@example.test"
    assert (mailer.host, mailer.port) == ("smtp.gmail.com", 587)
