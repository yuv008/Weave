"""What the agent surfaces share: graph access, config, stores, and how a tool context is built."""

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from agents.gate.principal import Principal, Surface
from agents.gate.resolve import Resolver
from agents.graph import ReadOnlyGraph
from agents.tools.base import ToolContext
from agents.tools.invoke import ToolCache
from shared.config import WeaveConfig


@dataclass
class Runtime:
    open_graph: Callable[[str], ReadOnlyGraph]
    cfg: WeaveConfig
    resolver: Resolver | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    pg_connect: Callable | None = None
    audit: Any = None
    memory: Any = None
    cache: ToolCache | None = field(default=None)

    def __post_init__(self) -> None:
        if self.cache is None:
            self.cache = ToolCache(self.cfg.answer.cache_ttl_seconds)

    def context(self, principal: Principal, agent: str = "answer", episode_id: str | None = None) -> ToolContext:
        recipient = None
        if self.resolver is not None:
            def recipient(person_id: str) -> Principal:
                return self.resolver.for_person(principal.tenant, person_id, Surface.SLACK_DM)
        return ToolContext(
            principal, self.open_graph(principal.tenant), self.cfg, clock=self.clock,
            pg_connect=self.pg_connect, memory=self.memory, recipient=recipient, agent=agent,
            episode_id=episode_id,
        )


def live_runtime() -> Runtime:
    """Runtime over FalkorDB Cloud and Neon, from the environment."""

    from agents.gate.stores import PostgresGateStore
    from agents.graph import open_company
    from agents.mcp.audit import PostgresAudit
    from agents.memory.store import PostgresMemoryStore
    from agents.mcp.server import TenantDirectory
    from shared.config import load_config
    from shared.env import load_env
    from agents.wisp.cue import _reuse
    from shared.graphdb import connect
    from shared.pg import connect_pg, warm_pg

    load_env()
    cfg = load_config()
    db = connect()

    def open_graph(tenant: str) -> ReadOnlyGraph:
        return open_company(tenant, db)

    gate = PostgresGateStore(connect_pg)
    resolver = Resolver(gate, TenantDirectory(open_graph), stale_minutes=cfg.acl_stale_minutes)
    memory = PostgresMemoryStore(connect_pg)
    try:
        warm_pg()
        gate.warm()
        memory.warm()
        _reuse(connect_pg)
    except Exception as exc:
        logging.getLogger(__name__).warning("cue connections were not warmed (%s)", type(exc).__name__)
    return Runtime(
        open_graph, cfg, resolver=resolver, pg_connect=connect_pg,
        audit=PostgresAudit(connect_pg), memory=memory,
    )
