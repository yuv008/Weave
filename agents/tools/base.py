"""What every tool shares: the call context, the result shape and the registry.

A tool takes a ``ToolContext`` plus its own arguments, never a user id or a
surface; the principal inside the context came from the gate resolver.
Every result carries node ids (so the next call starts from ids), citations
and the edges walked (the answer card's mini graph).
"""

import inspect
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from agents.gate.principal import Principal
from agents.gate.visible import gate_params
from agents.graph import ReadOnlyGraph
from shared.config import WeaveConfig
from shared.timefmt import parse_ts


class ToolInputError(ValueError):
    """Bad arguments. The message never mentions restricted data."""


@dataclass(frozen=True)
class Citation:
    item_id: str
    quote: str | None
    author: str | None
    at: str | None
    source: str | None
    url: str | None
    kind: str = "evidence"


@dataclass(frozen=True)
class RestrictedCitation:
    """A projection: the summary only, the source named as restricted, no quote and no link."""

    projection_id: str
    summary: str
    source: str = "restricted"
    kind: str = "projection"


@dataclass(frozen=True)
class PathEdge:
    src: str
    rel: str
    dst: str


@dataclass
class ToolResult:
    tool: str
    data: dict
    node_ids: list[str] = field(default_factory=list)
    citations: list[Citation | RestrictedCitation] = field(default_factory=list)
    path: list[PathEdge] = field(default_factory=list)
    message: str | None = None
    hidden: int = 0
    """Items, hops or edges filtered out. Always audited; shown only with ``hidden_sources_notice``."""

    def public(self, cfg: WeaveConfig) -> dict:
        out = {
            "tool": self.tool,
            "data": self.data,
            "node_ids": _unique(self.node_ids),
            "citations": [asdict(c) for c in _unique(self.citations)],
            "path": [asdict(e) for e in _unique(self.path)],
        }
        if self.message:
            out["message"] = self.message
        if cfg.hidden_sources_notice and self.hidden:
            out["hidden_count"] = self.hidden
        return out


def _unique(values: list) -> list:
    seen, out = set(), []
    for value in values:
        if value not in seen:
            seen.add(value)
            out.append(value)
    return out


@dataclass(frozen=True)
class ToolContext:
    principal: Principal
    graph: ReadOnlyGraph
    cfg: WeaveConfig
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    pg_connect: Callable | None = None
    embed: Callable[[str], list[float]] | None = None
    """Query embedder for the vector half of search; ``None`` until embeddings exist."""
    memory: Any = None
    """The agent memory store (``agents.memory.store.MemoryStore``); memory and propose tools need it."""
    recipient: Callable[[str], Principal] | None = None
    """Principal of another person on a private surface, for per-recipient filtering."""
    agent: str = "answer"
    """Memory namespace: (tenant, user, agent)."""
    episode_id: str | None = None
    """The answer episode this call belongs to, so proposals link back to it."""

    def __post_init__(self) -> None:
        if not isinstance(self.graph, ReadOnlyGraph):
            raise TypeError("tools read the graph only through ReadOnlyGraph")

    def params(self, **extra) -> dict:
        return {**gate_params(self.principal), **extra}

    def rows(self, cypher: str, **params) -> list:
        return self.graph.ro_query(cypher, self.params(**params)).result_set

    def likely(self, confidence: float | None) -> bool:
        return confidence is not None and confidence < self.cfg.likely_threshold

    def age_days(self, ts: str | None) -> float | None:
        if not ts:
            return None
        return (self.clock() - parse_ts(ts)).total_seconds() / 86400


def citation(row: dict) -> Citation:
    return Citation(
        item_id=row["id"],
        quote=row.get("quote"),
        author=row.get("author"),
        at=row.get("at"),
        source=row.get("source"),
        url=row.get("url"),
    )


@dataclass(frozen=True)
class ToolSpec:
    name: str
    fn: Callable[..., ToolResult]
    use_when: str
    dont_use_when: str
    args: dict[str, str] = field(default_factory=dict)
    choices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    mode: str = "read"
    """``read``: reads the graph only. ``memory``: also writes the caller's memory namespace.
    ``propose``: also writes proposal tasks for a person to confirm. No mode writes the graph."""

    @property
    def params(self) -> list[inspect.Parameter]:
        """The tool's own arguments: everything after ``ctx``."""

        return list(inspect.signature(self.fn).parameters.values())[1:]


REGISTRY: dict[str, ToolSpec] = {}
MODES = ("read", "memory", "propose")


def tool(
    name: str,
    *,
    use_when: str,
    dont_use_when: str,
    args: dict[str, str] | None = None,
    choices: dict[str, tuple[str, ...]] | None = None,
    mode: str = "read",
):
    """Register a tool. Descriptions follow "use when / do not use when" for MCP clients.

    ``args`` documents every argument (checked here, so a new argument cannot
    ship undocumented); ``choices`` lists allowed values for enum-like ones.
    """

    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}")

    def register(fn: Callable[..., ToolResult]) -> Callable[..., ToolResult]:
        if name in REGISTRY:
            raise ValueError(f"tool {name!r} registered twice")
        spec = ToolSpec(name, fn, use_when, dont_use_when, dict(args or {}), dict(choices or {}), mode)
        names = {p.name for p in spec.params}
        if set(spec.args) != names:
            raise ValueError(f"tool {name!r} must document exactly its arguments {sorted(names)}")
        if not set(spec.choices) <= names:
            raise ValueError(f"tool {name!r} has choices for unknown arguments")
        REGISTRY[name] = spec
        return fn

    return register
