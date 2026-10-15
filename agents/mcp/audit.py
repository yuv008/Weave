"""Audit log of agent tool calls and answers (``migrations/004_audit_log.sql``, ``005_agent_memory.sql``).

``record`` writes one row per tool call (``kind='tool'``); ``record_answer``
writes one row per answer (``kind='answer'``) with the tools it called;
``record_watch`` writes one row per impact-watcher recipient (``kind='watcher'``).
"""

import json
from collections.abc import Callable, Sequence
from typing import Protocol

from agents.gate.principal import Principal

_INSERT = """
INSERT INTO audit_log (
    tenant_id, request_id, surface, principal_kind, person_id, channel_group, level,
    visibility_hash, tool, args, node_ids, hidden, outcome, ms, kind, tools, conversation_id
) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::text[], %s, %s, %s, %s, %s::text[], %s)
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

    def record_answer(
        self,
        *,
        request_id: str,
        principal: Principal,
        question: str,
        intent: str,
        tools: list[str],
        node_ids: list[str],
        hidden: int,
        outcome: str,
        ms: int,
        conversation_id: str | None,
    ) -> None: ...

    def record_watch(
        self,
        *,
        request_id: str,
        principal: Principal,
        event_id: str,
        change: str,
        tools: list[str],
        node_ids: list[str],
        hidden: int,
        outcome: str,
        ms: int,
        task_ids: Sequence[str] = (),
        seen: int = 0,
    ) -> None: ...


class PostgresAudit:
    def __init__(self, connect: Callable):
        self._connect = connect

    def _insert(self, principal: Principal, request_id: str, tool: str, args: dict, node_ids, hidden, outcome,
                ms, kind: str, tools: list[str], conversation_id: str | None) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(_INSERT, (
                    principal.tenant, request_id, str(principal.surface), str(principal.kind),
                    principal.user_id, principal.channel_group, principal.level,
                    principal.visibility_hash, tool, json.dumps(args, default=str),
                    list(dict.fromkeys(node_ids)), hidden, outcome, ms, kind, list(tools), conversation_id,
                ))
            conn.commit()
        finally:
            conn.close()

    def record(self, *, request_id, principal, tool, args, node_ids, hidden, outcome, ms) -> None:
        self._insert(principal, request_id, tool, args, node_ids, hidden, outcome, ms, "tool", [], None)

    def record_answer(self, *, request_id, principal, question, intent, tools, node_ids, hidden, outcome, ms,
                      conversation_id) -> None:
        self._insert(principal, request_id, "answer", {"intent": intent, "question": question}, node_ids, hidden,
                     outcome, ms, "answer", tools, conversation_id)

    def record_watch(self, *, request_id, principal, event_id, change, tools, node_ids, hidden, outcome, ms,
                     task_ids=(), seen=0) -> None:
        self._insert(principal, request_id, "watcher",
                     {"event_id": event_id, "change": change, "tasks": list(task_ids), "seen": seen},
                     node_ids, hidden, outcome, ms, "watcher", tools, None)


class MemoryAudit:
    """For tests."""

    def __init__(self):
        self.rows: list[dict] = []

    def record(self, **entry) -> None:
        self.rows.append({"kind": "tool", **entry})

    def record_answer(self, **entry) -> None:
        self.rows.append({"kind": "answer", "tool": "answer", **entry})

    def record_watch(self, **entry) -> None:
        self.rows.append({"kind": "watcher", "tool": "watcher", "task_ids": [], "seen": 0, **entry})
