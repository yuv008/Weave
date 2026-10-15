"""Shared setup for tool tests: a scratch FalkorDB graph seeded from the fixture, and per-person contexts.

Principals come from the in-memory gate store, so graph tool tests need
FalkorDB only. They run with WEAVE_LIVE_TESTS=1.
"""

import os
import uuid

import pytest

from agents.gate.resolve import Resolver
from agents.graph import ReadOnlyGraph
from agents.tools.base import ToolContext
from fixtures.acme import Fixture, build
from shared.config import WeaveConfig, load_config
from tests.gate_support import NOW, person_principal

live = pytest.mark.skipif(os.environ.get("WEAVE_LIVE_TESTS") != "1", reason="set WEAVE_LIVE_TESTS=1")


@pytest.fixture(scope="module")
def scratch():
    """(fixture, raw writable graph for test setup, read-only graph for tools)."""

    from shared.env import load_env

    load_env()
    from fixtures.seed import seed_graph
    from shared.graphdb import connect

    fx = build("acme")
    name = f"co:test_{uuid.uuid4().hex}"
    raw = connect().select_graph(name)
    seed_graph(raw, fx)
    try:
        yield fx, raw, ReadOnlyGraph(raw, name)
    finally:
        raw.delete()


def make_ctx(
    fx: Fixture,
    graph: ReadOnlyGraph,
    who: str,
    *,
    cfg: WeaveConfig | None = None,
    resolver: Resolver | None = None,
    clock=lambda: NOW,
    **extra,
) -> ToolContext:
    return ToolContext(person_principal(fx, who, resolver), graph, cfg or load_config(), clock=clock, **extra)
