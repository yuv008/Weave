"""Live gate data: Postgres for tokens and the ACL snapshot; the graph for people."""

from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime

from agents.gate.resolve import AclSnapshot, PersonInfo
from agents.graph import ReadOnlyGraph
from shared.schema import Label, Prop, Rel

_PERSON = f"""
MATCH (p:{Label.PERSON} {{{Prop.ID}: $id}})
OPTIONAL MATCH (s:{Label.PERSON})-[:{Rel.REPORTS_TO}*1..6]->(p)
RETURN p.{Prop.LEVEL}, p.{Prop.DEPT}, collect(DISTINCT s.{Prop.ID})
"""

_LEVELS = f"""
MATCH (p:{Label.PERSON})
WHERE p.{Prop.ID} IN $ids
RETURN p.{Prop.ID}, p.{Prop.LEVEL}
"""


class PostgresGateStore:
    """One short pooled connection per call; the gate reads fresh ACL state per request."""

    def __init__(self, connect: Callable):
        self._connect = connect

    def token_owner(self, token_sha256: str) -> tuple[str, str] | None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tenant_id, person_id FROM api_token WHERE token_sha256 = %s AND revoked_at IS NULL",
                    (token_sha256,),
                )
                row = cur.fetchone()
        finally:
            conn.close()
        return (row[0], row[1]) if row else None

    def snapshot(self, tenant: str) -> AclSnapshot:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT person_id, acl_group FROM group_membership WHERE tenant_id = %s", (tenant,))
                members = cur.fetchall()
                cur.execute("SELECT acl_group, last_good_sync_at FROM source_sync WHERE tenant_id = %s", (tenant,))
                syncs = cur.fetchall()
                cur.execute("SELECT acl_epoch FROM tenant_acl WHERE tenant_id = %s", (tenant,))
                epoch = cur.fetchone()
        finally:
            conn.close()

        memberships: dict[str, set[str]] = defaultdict(set)
        for person_id, group in members:
            memberships[person_id].add(group)
        last_sync: dict[str, datetime] = {}
        for group, synced in syncs:
            if group not in last_sync or synced > last_sync[group]:
                last_sync[group] = synced
        return AclSnapshot(dict(memberships), last_sync, int(epoch[0]) if epoch else 0)


class GraphDirectory:
    def __init__(self, graph: ReadOnlyGraph):
        self._graph = graph

    def person(self, tenant: str, person_id: str) -> PersonInfo | None:
        rows = self._graph.ro_query(_PERSON, {"id": person_id}).result_set
        if not rows or rows[0][0] is None:
            return None
        level, dept, subordinates = rows[0]
        return PersonInfo(person_id, int(level), dept, tuple(s for s in subordinates if s))

    def levels(self, tenant: str, person_ids: Iterable[str]) -> dict[str, int]:
        rows = self._graph.ro_query(_LEVELS, {"ids": list(person_ids)}).result_set
        return {person_id: int(level) for person_id, level in rows}
