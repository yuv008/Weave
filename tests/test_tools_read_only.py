"""Agents cannot write the graph: read tools only read; memory and propose tools write only through the memory store."""

import re
from pathlib import Path

import pytest

from agents.graph import ReadOnlyGraph
from agents.tools import REGISTRY, ToolContext
from agents.tools.base import MODES, Citation, PathEdge, RestrictedCitation, ToolResult
from agents.tools.decisions import _as_of
from agents.tools.search import fuse
from fixtures.acme import build
from shared.config import load_config
from tests.gate_support import person_principal

TOOLS_DIR = Path(__file__).resolve().parent.parent / "agents"
EXPECTED = {
    "search_context": "read", "get_decision": "read", "trace_decision": "read", "find_owner": "read",
    "get_figures": "read", "get_dependencies": "read", "impact_of": "read", "dept_rollup": "read",
    "remember": "memory", "recall_memory": "memory", "draft_email": "propose", "notify_owner": "propose",
}
WRITE_SQL = re.compile(r"\b(INSERT|UPDATE|DELETE|MERGE|CREATE|DROP|ALTER|TRUNCATE)\b")


def test_every_catalogue_tool_is_registered_with_its_mode():
    assert {name: spec.mode for name, spec in REGISTRY.items()} == EXPECTED
    for spec in REGISTRY.values():
        assert spec.mode in MODES
        assert spec.use_when and spec.dont_use_when


def test_tool_modules_have_no_write_statements_and_no_direct_database_writes():
    tools = TOOLS_DIR / "tools"
    for path in tools.glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not WRITE_SQL.search(text), f"{path} contains a write statement"
        assert ".commit(" not in text, path
    for name in ("memory.py", "drafts.py"):
        text = (tools / name).read_text(encoding="utf-8")
        assert "pg_connect" not in text and "connect_pg" not in text, name


def test_no_agent_module_can_write_the_graph():
    write_call = re.compile(r"(?<!ro_)\bquery\(")
    scanned = set()
    for path in TOOLS_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert not write_call.search(text), f"{path} calls query()"
        assert "import falkordb" not in text and "from falkordb" not in text, path
        scanned.add(path.relative_to(TOOLS_DIR).as_posix())
    assert {"tools/base.py", "tools/drafts.py", "tools/memory.py", "mcp/server.py", "mcp/__main__.py",
            "memory/store.py", "answer/agent.py", "answer/loop.py"} <= scanned


def test_tool_context_requires_read_only_graph():
    fx = build("acme")
    principal = person_principal(fx, "sam")
    with pytest.raises(TypeError):
        ToolContext(principal, object(), load_config())
    ctx = ToolContext(principal, ReadOnlyGraph(object(), "co:acme"), load_config())
    assert not hasattr(ctx.graph, "query")


def test_tools_take_no_user_or_surface_arguments():
    import inspect

    for spec in REGISTRY.values():
        params = set(inspect.signature(spec.fn).parameters)
        assert not params & {"user", "user_id", "person_id", "surface", "principal", "level", "groups"}, spec.name


def test_public_result_hides_count_unless_enabled():
    cfg = load_config()
    result = ToolResult(
        "get_decision", {"x": 1}, node_ids=["a", "a", "b"],
        citations=[Citation("i", "q", "Dev", None, "#eng", "u"), RestrictedCitation("p", "summary")],
        path=[PathEdge("a", "EVIDENCED_BY", "i")] * 2, hidden=3,
    )
    public = result.public(cfg)
    assert public["node_ids"] == ["a", "b"] and len(public["path"]) == 1
    assert "hidden_count" not in public
    assert public["citations"][1] == {"projection_id": "p", "summary": "summary", "source": "restricted", "kind": "projection"}
    assert result.public(cfg.model_copy(update={"hidden_sources_notice": True}))["hidden_count"] == 3


def test_fuse_and_as_of_parsing():
    assert [item for item, _ in fuse(["a", "b"], ["b", "c"])] == ["b", "a", "c"]
    assert _as_of("2026-10-05") == "2026-10-05T23:59:59+00:00"
    assert _as_of(None) is None
