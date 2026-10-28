"""Owner invites, members joining, and removing a member."""

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
OWNER = {"name": "Ada", "email": "ada@north.test", "password": "correct-horse", "company": "North"}


class _Tokens:
    """api_token writes from /setup/open and the member removal, recorded."""

    def __init__(self):
        self.calls = []
        self.rowcount = 1

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.calls.append((" ".join(sql.split()), tuple(params or ())))

    def commit(self):
        pass

    def close(self):
        pass


def _team(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    monkeypatch.setenv("WEAVE_PUBLIC_URL", "https://weave.test")
    mailer, tokens = MemoryMailer(), _Tokens()
    app = create_app(tenant_id="real", store=MemoryStore(), secrets=MemorySecretStore(),
                     accounts=MemoryAccountStore(), mailer=mailer)
    app.state.pg_connect = lambda: tokens
    owner = TestClient(app)
    owner.post("/signup", data=OWNER)
    app.state.secrets.put("north", "slack", "TNORTH", {"token": "xoxb-north"}, status="connected")
    return app, owner, mailer, tokens


def _invite(owner, mailer, email):
    assert owner.post("/setup/invites", json={"email": email}).json() == {"invited": email}
    found = re.search(r"https://weave\.test/invite\?token=(\S+)", mailer.sent[-1]["text"])
    assert mailer.sent[-1]["to"] == email and "Ada invited you to North" in mailer.sent[-1]["text"]
    return urllib.parse.unquote(found.group(1))


def test_an_invited_person_joins_as_a_member_and_cannot_run_the_company(monkeypatch):
    app, owner, mailer, _tokens = _team(monkeypatch)
    token = _invite(owner, mailer, "bo@north.test")
    member = TestClient(app)

    assert member.get(f"/invite/info?token={urllib.parse.quote(token)}").json() == {"email": "bo@north.test", "company": "North"}
    joined = member.post("/invite/join", data={"token": token, "name": "Bo", "password": "another-long-one"}, follow_redirects=False)
    assert joined.headers["location"] == "/setup"
    session = member.get("/session").json()
    assert (session["tenant"], session["role"], session["verified"]) == ("north", "member", True)

    # Members use Weave; they do not change the company.
    assert member.post("/setup/open").status_code == 200
    for path, body in (("/setup/model", {"key": "gsk"}), ("/setup/company", {"name": "Other"}),
                       ("/setup/invites", {"email": "x@north.test"}), ("/setup/github/repo", {"owner": "o", "repo": "r"})):
        assert member.post(path, json=body).status_code == 403, path
    assert member.get("/setup/members").status_code == 403
    assert member.get("/connect/slack", follow_redirects=False).headers["location"] == "/setup?error=owner"

    team = owner.get("/setup/members").json()
    assert [(person["email"], person["role"]) for person in team["members"]] == [("ada@north.test", "owner"), ("bo@north.test", "member")]
    assert team["invites"] == []
    # The link worked once.
    again = TestClient(app).post("/invite/join", data={"token": token, "name": "Eve", "password": "another-long-one"}, follow_redirects=False)
    assert "error=expired" in again.headers["location"]
    use_store(None)


def test_an_existing_account_accepts_and_switches_company(monkeypatch):
    app, owner, mailer, _tokens = _team(monkeypatch)
    other = TestClient(app)
    other.post("/signup", data={"name": "Cy", "email": "cy@south.test", "password": "correct-horse", "company": "South"})
    token = _invite(owner, mailer, "cy@south.test")

    accepted = other.post("/invite/accept", data={"token": token}, follow_redirects=False)
    assert accepted.headers["location"] == "/setup"
    session = other.get("/session").json()
    assert session["tenant"] == "north" and session["role"] == "member"
    assert {item["tenant"] for item in session["memberships"]} == {"north", "south"}
    use_store(None)


def test_an_invite_is_refused_to_a_different_signed_in_address(monkeypatch):
    app, owner, mailer, _tokens = _team(monkeypatch)
    token = _invite(owner, mailer, "bo@north.test")
    stranger = TestClient(app)
    stranger.post("/signup", data={"name": "Eve", "email": "eve@else.test", "password": "correct-horse", "company": "Else"})

    refused = stranger.post("/invite/accept", data={"token": token}, follow_redirects=False)
    assert "error=email" in refused.headers["location"]
    assert stranger.get("/session").json()["tenant"] == "else"
    # Not signed in at all: told to sign in.
    assert "error=signin" in TestClient(app).post("/invite/accept", data={"token": token}, follow_redirects=False).headers["location"]
    use_store(None)


def test_removing_a_member_ends_their_sessions_and_tokens_here(monkeypatch):
    app, owner, mailer, tokens = _team(monkeypatch)
    token = _invite(owner, mailer, "bo@north.test")
    member = TestClient(app)
    member.post("/invite/join", data={"token": token, "name": "Bo", "password": "another-long-one"})
    bo = next(person for person in owner.get("/setup/members").json()["members"] if person["role"] == "member")

    assert owner.post("/setup/members/remove", json={"user_id": bo["user_id"]}).json() == {"removed": "bo@north.test"}
    assert member.get("/setup/status").status_code == 401
    revoke = tokens.calls[-1]
    assert revoke[0].startswith("UPDATE api_token SET revoked_at = now() WHERE tenant_id = %s AND person_id = %s")
    assert revoke[1] == ("north", "north:person:bo@north.test")
    # The owner cannot be removed.
    me = next(person for person in owner.get("/setup/members").json()["members"] if person["role"] == "owner")
    assert owner.post("/setup/members/remove", json={"user_id": me["user_id"]}).status_code == 400
    use_store(None)


def test_a_cancelled_invite_stops_working_and_members_cannot_be_invited_twice(monkeypatch):
    app, owner, mailer, _tokens = _team(monkeypatch)
    token = _invite(owner, mailer, "bo@north.test")
    pending = owner.get("/setup/members").json()["invites"]
    assert [invite["email"] for invite in pending] == ["bo@north.test"]

    assert owner.post("/setup/invites/revoke", json={"id": pending[0]["id"]}).json() == {"revoked": True}
    assert TestClient(app).get(f"/invite/info?token={urllib.parse.quote(token)}").status_code == 410
    assert owner.post("/setup/invites", json={"email": "ada@north.test"}).status_code == 400
    assert owner.post("/setup/invites", json={"email": "not-an-email"}).status_code == 400
    use_store(None)
