"""Each company's own Groq key, the shared-key daily cap, and the /setup/model route."""

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from agents.answer import llm as llm_module
from agents.answer.llm import GroqLLM, KeyRejected, TenantKeyLLM, using_tenant
from app.connect import accounts
from app.connect.credentials import use_store
from app.connect.secrets import MemorySecretStore
from app.ingest.records import MemoryStore
from app.main import create_app

accounts.SKIP_GRAPH = True


def _reply(status=200):
    def http(url, headers, body):
        http.seen.append(headers["Authorization"])
        if status != 200:
            return status, json.dumps({"error": {"message": "invalid api key"}}).encode()
        return 200, json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    http.seen = []
    return http


def _llm(keys, http):
    shared = GroqLLM(model="m", api_key="gsk-server", http=http, own_bucket=True)
    return TenantKeyLLM(shared, key_for=lambda tenant: keys.get(tenant, ""))


def test_a_company_key_is_used_only_for_that_company():
    http = _reply()
    llm = _llm({"north": "gsk-north"}, http)

    with using_tenant("north"):
        llm.complete([{"role": "user", "content": "hi"}])
    with using_tenant("south"):
        llm.complete([{"role": "user", "content": "hi"}])
    llm.complete([{"role": "user", "content": "hi"}])

    assert http.seen == ["Bearer gsk-north", "Bearer gsk-server", "Bearer gsk-server"]
    north, server = llm.client_for("north"), llm.client_for("south")
    assert north is not server and north._bucket is not server._bucket
    assert "gsk-north" not in repr(llm)


def test_a_rejected_company_key_fails_and_never_falls_back(monkeypatch):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    secrets = MemorySecretStore()
    secrets.put("north", "groq", "groq", {"token": "gsk-north"}, status="connected")
    use_store(secrets)
    http = _reply(status=401)
    llm = _llm({"north": "gsk-north"}, http)
    try:
        with using_tenant("north"), pytest.raises(KeyRejected):
            llm.complete([{"role": "user", "content": "hi"}])
        assert http.seen == ["Bearer gsk-north"]
        assert secrets.get("north", "groq")["status"] == "failed"
    finally:
        use_store(None)


def test_answer_and_mail_run_inside_the_principal_company():
    from agents.answer.agent import AnswerAgent
    from agents.wisp import mail as mail_module

    seen = []
    agent = AnswerAgent.__new__(AnswerAgent)
    agent._answer = lambda principal, question, **kwargs: seen.append(llm_module._TENANT.get())
    principal = SimpleNamespace(tenant="north")
    agent.answer(principal, "q")

    original = mail_module._mail
    mail_module._mail = lambda *args: seen.append(llm_module._TENANT.get()) or {}
    try:
        mail_module.mail(None, None, principal, "to", "subject", "body")
    finally:
        mail_module._mail = original
    assert seen == ["north", "north"]
    assert llm_module._TENANT.get() is None


class _Usage:
    def __init__(self):
        self.calls = {}

    def __call__(self):
        return self

    def cursor(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params):
        self.tenant = params[0]
        self.calls[self.tenant] = self.calls.get(self.tenant, 0) + 1

    def fetchone(self):
        return (self.calls[self.tenant],)

    def commit(self):
        pass

    def close(self):
        pass


def _runtime(cap=3, provider="groq"):
    llm = SimpleNamespace(provider=provider, shared_daily_cap=cap)
    return SimpleNamespace(cfg=SimpleNamespace(answer=SimpleNamespace(llm=llm)), pg_connect=_Usage())


def test_the_shared_key_cap_counts_only_companies_on_the_shared_key(monkeypatch):
    from agents.mcp.server import _shared_cap_reached

    monkeypatch.setenv("WEAVE_TENANT", "real")
    monkeypatch.setattr(llm_module, "saved_key", lambda tenant: "gsk-own" if tenant == "own" else "")
    runtime = _runtime(cap=3)

    assert [_shared_cap_reached(runtime, "north") for _ in range(4)] == [False, False, False, True]
    assert not any(_shared_cap_reached(runtime, "real") for _ in range(5))
    assert not any(_shared_cap_reached(runtime, "own") for _ in range(5))
    assert runtime.pg_connect.calls == {"north": 4}
    assert _shared_cap_reached(_runtime(provider="none"), "north") is False


def _signed_in(monkeypatch, check):
    monkeypatch.setenv("WEAVE_SECRET_KEY", "test-key")
    app = create_app(tenant_id="real", store=MemoryStore(), secrets=MemorySecretStore())
    app.state.groq_check = check
    client = TestClient(app)
    client.post("/signup", data={"name": "Ada", "email": "ada@north.test", "password": "correct-horse", "company": "North"})
    return app, client


def test_setup_checks_a_key_before_saving_and_can_remove_it(monkeypatch):
    app, client = _signed_in(monkeypatch, lambda key: key == "gsk-good")

    rejected = client.post("/setup/model", json={"key": "gsk-bad"})
    assert rejected.status_code == 400 and "rejected" in rejected.json()["error"]
    assert app.state.secrets.get("north", "groq") is None
    assert client.get("/setup/status").json()["model"] == "shared"

    assert client.post("/setup/model", json={"key": "gsk-good"}).json() == {"model": "own"}
    assert client.get("/setup/status").json()["model"] == "own"

    assert client.post("/setup/model", json={"remove": True}).json() == {"model": "shared"}
    assert app.state.secrets.get("north", "groq") is None
    assert client.post("/setup/model", json={"key": ""}).status_code == 400
    use_store(None)


def test_setup_says_so_when_groq_cannot_be_reached(monkeypatch):
    def unreachable(key):
        raise OSError("network down")

    app, client = _signed_in(monkeypatch, unreachable)
    response = client.post("/setup/model", json={"key": "gsk-good"})
    assert response.status_code == 503
    assert app.state.secrets.get("north", "groq") is None
    use_store(None)
