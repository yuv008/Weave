"""Flow 1 over real HTTP: a uvicorn MCP server on the scratch graph, called with the SDK's own client."""

import socket
import threading
import time

import anyio
import httpx2
import pytest
import uvicorn
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from agents.mcp.audit import MemoryAudit
from agents.mcp.server import build_app
from agents.runtime import Runtime
from shared.config import load_config
from tests.gate_support import NOW, fixture_resolver
from tests.tool_support import live, scratch  # noqa: F401

pytestmark = live


@pytest.fixture(scope="module")
def served(scratch):
    fx, _, graph = scratch
    resolver, store = fixture_resolver(fx)
    for who in ("sam", "priya"):
        store.add_token(f"tok-{who}", fx.ids[who])
    audit = MemoryAudit()
    app = build_app(Runtime(lambda tenant: graph, load_config(), resolver=resolver, clock=lambda: NOW, audit=audit))
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 20
    while not server.started:
        assert time.monotonic() < deadline, "MCP server did not start"
        time.sleep(0.05)
    try:
        yield fx, f"http://127.0.0.1:{port}/mcp", audit
    finally:
        server.should_exit = True
        thread.join(timeout=10)


def call(url, token, name, arguments):
    async def run():
        headers = {"Authorization": f"Bearer {token}"}
        async with httpx2.AsyncClient(headers=headers, timeout=30) as http:
            async with Client(streamable_http_client(url, http_client=http)) as client:
                tools = await client.list_tools()
                result = await client.call_tool(name, arguments)
                return [t.name for t in tools.tools], result

    return anyio.run(run)


def test_flow1_sam_gets_provider_b_citing_the_slack_thread(served):
    fx, url, audit = served
    names, result = call(url, "tok-sam", "get_decision", {"topic": "SSO"})
    assert "get_decision" in names and result.is_error is not True
    body = result.structured_content
    [card] = body["data"]["decisions"]
    assert card["statement"] == "Use Provider B for SSO."
    assert card["replaces"]["statement"] == "Use Provider A for SSO."
    slack = [c for c in body["citations"] if c["item_id"] == fx.ids["item_eng_switch_b"]]
    assert slack and slack[0]["author"] == "Dev" and slack[0]["quote"] and slack[0]["at"]
    assert "slack.com" in slack[0]["url"]
    row = audit.rows[-1]
    assert row["principal"].user_id == fx.ids["sam"] and row["tool"] == "get_decision" and row["outcome"] == "ok"


def test_priya_gets_roadmap_evidence_only_over_mcp(served):
    fx, url, audit = served
    _, result = call(url, "tok-priya", "get_decision", {"topic": "SSO"})
    body = result.structured_content
    [card] = body["data"]["decisions"]
    assert card["evidence"] == [fx.ids["item_roadmap_issue"]] and card["replaces"] is None
    assert "hidden_count" not in body
    assert fx.ids["item_eng_switch_b"] not in str(body)
    row = audit.rows[-1]
    assert row["principal"].user_id == fx.ids["priya"] and row["hidden"] >= 2


def test_bad_token_is_rejected_over_http(served):
    _, url, audit = served
    before = len(audit.rows)
    response = httpx2.post(
        url,
        json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
        headers={"Authorization": "Bearer tok-nobody", "Accept": "application/json, text/event-stream"},
    )
    assert response.status_code == 401 and response.text == "not authorized"
    assert len(audit.rows) == before
