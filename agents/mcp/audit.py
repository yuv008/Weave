"""Audit log of agent tool calls (``migrations/004_audit_log.sql``)."""

import json
from collections.abc import Callable
from typing import Protocol

from agents.gate.principal import Principal

_INSERT = """
INSERT INTO audit_log (
    tenant_id, request_id, surface, principal_kind, person_id, channel_group, level,
    visibility_hash, tool, args, node_ids, hidden, outcome, ms
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s)
"""


class Audit(Protocol):
    def record(
        self,
        *,
        request_id: str,
        principal: Principal,
        tool: str,
        args: dict,
        node_ids: list[str],
        hidden: int,
        outcome: str,
        ms: int,
    ) -> None: ...


class PostgresAudit:
    def __init__(self, connect: Callable):
        self._connect = connect

    def record(self, *, request_id, principal, tool, args, node_ids, hidden, outcome, ms) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(_INSERT, (
                    principal.tenant, request_id, str(principal.surface), str(principal.kind),
                    principal.user_id, principal.channel_group, principal.level,
                    principal.visibility_hash, tool, json.dumps(args, default=str),
                    list(dict.fromkeys(node_ids)), hidden, outcome, ms,
                ))
            conn.commit()
        finally:
            conn.close()


class MemoryAudit:
    """For tests."""

    def __init__(self):
        self.rows: list[dict] = []

    def record(self, **entry) -> None:
        self.rows.append(entry)
