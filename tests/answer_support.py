"""Offline setup for answer-agent tests: a stub graph that knows the fixture's subjects, and a runtime."""

from types import SimpleNamespace

from agents.answer.subjects import MATCH_LABELS
from agents.gate.principal import Surface
from agents.graph import ReadOnlyGraph
from agents.memory.store import InMemoryMemoryStore
from agents.mcp.audit import MemoryAudit
from agents.runtime import Runtime
from fixtures.acme import Fixture, build
from shared.config import load_config
from shared.schema import Prop
from tests.gate_support import NOW, fixture_resolver


class SubjectGraph:
    """Answers the subject-matching query from the fixture; every other query finds nothing."""

    def __init__(self, fx: Fixture):
        self.rows = [
            [n.id, n.props.get(Prop.NAME), n.props.get(Prop.ALIASES, []), list(n.labels)]
            for n in fx.nodes
            if any(label in n.labels for label in MATCH_LABELS)
        ]
        self.queries: list[str] = []

    def ro_query(self, query, params=None):
        self.queries.append(query)
        if "labels(n)" in query and f"n.{Prop.ALIASES}" in query and "MATCH (n) WHERE" in query:
            return SimpleNamespace(result_set=[list(r) for r in self.rows])
        return SimpleNamespace(result_set=[])


def rule_config(cfg=None):
    """The given config on the rule-based agent, so tests do not call Groq."""

    cfg = cfg or load_config()
    if cfg.answer.llm.provider == "none":
        return cfg
    llm = cfg.answer.llm.model_copy(update={"provider": "none"})
    return cfg.model_copy(update={"answer": cfg.answer.model_copy(update={"llm": llm})})


def offline_runtime(fx: Fixture | None = None, **overrides):
    fx = fx or build("acme")
    resolver, store = fixture_resolver(fx)
    graph = ReadOnlyGraph(SubjectGraph(fx), "co:acme")
    runtime = Runtime(
        lambda tenant: graph, rule_config(overrides.pop("cfg", None)), resolver=resolver, clock=lambda: NOW,
        audit=MemoryAudit(), memory=InMemoryMemoryStore(clock=lambda: NOW), **overrides,
    )
    return SimpleNamespace(fx=fx, runtime=runtime, resolver=resolver, store=store, graph=graph)


def principal(env, who: str, surface: Surface = Surface.WEB):
    return env.resolver.for_person(env.fx.tenant, env.fx.ids[who], surface)
