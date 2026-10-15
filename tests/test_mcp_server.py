"""The MCP server offline: bearer auth, the tool list Cursor sees, argument checks, error masking, audit."""

import inspect
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from agents.graph import ReadOnlyGraph
from agents.mcp.audit import MemoryAudit
from agents.mcp.server import build_app
from agents.tools import REGISTRY
from fixtures.acme import build
from shared.config import load_config
from tests.gate_support import NOW, fixture_resolver

HEADERS = {
    "accept": "application/json, text/event-stream",
    "content-type": "application/json",
    "mcp-protocol-version": "2025-06-18",
}
FORBIDDEN = {"user", "user_id", "person", "person_id", "tenant", "surface", "principal", "level", "groups"}


class EmptyGraph:
    def ro_query(self, query, params=None):
        return SimpleNamespace(result_set=[])


@pytest.fixture()
def server():
    fx = build("acme")
    resolver, store = fixture_resolver(fx)
    store.add_token("tok-sam", fx.ids["sam"])
    store.add_token("tok-priya", fx.ids["priya"])
    audit, opened = MemoryAudit(), []

    def open_graph(tenant):
        opened.append(tenant)
        return ReadOnlyGraph(EmptyGraph(), f"co:{tenant}")

    app = build_app(resolver, open_graph, load_config(), clock=lambda: NOW, audit=audit)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        yield SimpleNamespace(fx=fx, store=store, audit=audit, opened=opened, client=client)


def rpc(server, method, params=None, token="tok-sam", request_id=1):
    headers = dict(HEADERS)
    if token is not None:
        headers["authorization"] = f"Bearer {token}"
    body = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        body["params"] = params
    return server.client.post("/mcp", json=body, headers=headers)


def call(server, name, arguments, token="tok-sam"):
    response = rpc(server, "tools/call", {"name": name, "arguments": arguments}, token=token)
    assert response.status_code == 200
    return response.json()["result"]


def test_requests_without_a_valid_token_get_the_same_401(server):
    server.store.tokens.pop(next(k for k, v in server.store.tokens.items() if v == server.fx.ids["priya"]))
    responses = [
        rpc(server, "tools/list", token=None),
        rpc(server, "tools/list", token=""),
        rpc(server, "tools/list", token="tok-nobody"),
        rpc(server, "tools/list", token="tok-priya"),
        server.client.post("/mcp", json={}, headers={**HEADERS, "authorization": "Basic dG9rLXNhbQ=="}),
    ]
    for response in responses:
        assert response.status_code == 401
        assert response.text == "not authorized"
        assert response.headers["www-authenticate"] == "Bearer"
    assert server.audit.rows == []


def test_tool_list_is_the_registry_with_use_when_descriptions(server):
    response = rpc(server, "tools/list")
    assert response.status_code == 200
    tools = {t["name"]: t for t in response.json()["result"]["tools"]}
    assert set(tools) == set(REGISTRY)
    for name, tool in tools.items():
        assert "\nUse when: " in tool["description"] and "\nDo not use when: " in tool["description"], name
        assert tool["annotations"]["readOnlyHint"] is True
        assert tool["annotations"]["destructiveHint"] is False
        schema = tool["inputSchema"]
        assert not set(schema["properties"]) & FORBIDDEN, name
        assert all(prop["description"] for prop in schema["properties"].values()), name
        spec = REGISTRY[name]
        assert list(schema["properties"]) == [p.name for p in spec.params]
        required = [p.name for p in spec.params if p.default is inspect.Parameter.empty]
        assert schema.get("required", []) == required, name
    assert tools["get_dependencies"]["inputSchema"]["properties"]["direction"]["enum"] == ["both", "downstream", "upstream"]


@pytest.mark.parametrize("arguments, message", [
    ({"bogus": 1}, "unknown arguments"),
    ({"topic": 5}, "topic must be a string"),
    ({}, "give a topic, a repo or a decision_id"),
])
def test_bad_arguments_are_tool_errors_without_internals(server, arguments, message):
    result = call(server, "get_decision", arguments)
    assert result["isError"] is True
    [content] = result["content"]
    assert message in content["text"] and "Traceback" not in content["text"]
    assert server.audit.rows[-1]["outcome"] == "bad_input"


def test_enum_and_required_checks(server):
    assert "direction must be one of" in call(server, "get_dependencies", {"entity": "x", "direction": "sideways"})["content"][0]["text"]
    assert "missing arguments ['query']" in call(server, "search_context", {})["content"][0]["text"]
    assert "unknown tool" in call(server, "write_graph", {})["content"][0]["text"]


def test_call_runs_as_the_token_owner_in_the_token_tenant(server):
    result = call(server, "get_decision", {"topic": "SSO"})
    assert result.get("isError") is not True
    assert result["structuredContent"]["tool"] == "get_decision"
    assert "search_context" in result["structuredContent"]["message"]
    assert server.opened == ["acme"]
    [row] = server.audit.rows
    assert row["principal"].user_id == server.fx.ids["sam"]
    assert row["principal"].surface == "mcp" and row["outcome"] == "ok"
    assert row["args"] == {"topic": "SSO"} and isinstance(row["hidden"], int)


def test_each_request_resolves_its_own_principal(server):
    call(server, "find_owner", {"subject": "SSO"}, token="tok-sam")
    call(server, "find_owner", {"subject": "SSO"}, token="tok-priya")
    call(server, "find_owner", {"subject": "SSO"}, token="tok-sam")
    users = [row["principal"].user_id for row in server.audit.rows]
    assert users == [server.fx.ids["sam"], server.fx.ids["priya"], server.fx.ids["sam"]]
    assert len({row["request_id"] for row in server.audit.rows}) == 3


def test_unexpected_errors_are_masked(server):
    def broken(tenant):
        raise RuntimeError("password=hunter2 at falkor://secret-host")

    fx = server.fx
    resolver, store = fixture_resolver(fx)
    store.add_token("tok-sam", fx.ids["sam"])
    audit = MemoryAudit()
    app = build_app(resolver, broken, load_config(), clock=lambda: NOW, audit=audit)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "get_decision", "arguments": {"topic": "SSO"}}},
            headers={**HEADERS, "authorization": "Bearer tok-sam"},
        )
    text = response.json()["result"]["content"][0]["text"]
    assert text.startswith("internal error (request ") and "hunter2" not in text and "secret-host" not in text
    assert audit.rows[0]["outcome"] == "error"


def test_audit_failure_does_not_break_the_answer(server):
    class Broken:
        def record(self, **entry):
            raise RuntimeError("db down")

    fx = server.fx
    resolver, store = fixture_resolver(fx)
    store.add_token("tok-sam", fx.ids["sam"])
    open_graph = lambda tenant: ReadOnlyGraph(EmptyGraph(), f"co:{tenant}")  # noqa: E731
    app = build_app(resolver, open_graph, load_config(), clock=lambda: NOW, audit=Broken())
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": "find_owner", "arguments": {"subject": "SSO"}}},
            headers={**HEADERS, "authorization": "Bearer tok-sam"},
        )
    assert response.json()["result"].get("isError") is not True
