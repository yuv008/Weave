"""Live gate data: Postgres for tokens and the ACL snapshot; the graph for people."""

import threading
from collections import defaultdict
from collections.abc import Callable, Iterable
from datetime import datetime

from agents.gate.resolve import AclSnapshot, PersonInfo
from agents.graph import ReadOnlyGraph
from shared.pg import ASSUME_APP_ROLE, SET_TENANT, use_tenant
from shared.schema import Label, Prop, Rel

TOKEN_OWNER_SQL = "SELECT tenant_id, person_id FROM weave_token_owner(%s)"

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
    """Fresh ACL reads on one kept connection. A new handshake is too slow for a draft cue."""

    def __init__(self, connect: Callable):
        self._connect = connect
        self._lock = threading.Lock()
        self._conn = None

    def warm(self) -> None:
        """Open the kept connection before the first cue."""

        def ping(conn):
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
                cur.fetchone()

        self._using(ping)

    def _borrow(self):
        conn = self._conn
        flag = None if conn is None else getattr(conn, "closed", None)
        if conn is not None and flag is not None and not bool(flag):
            return conn, True
        conn = self._connect()
        keep = getattr(conn, "closed", None) is not None and not bool(getattr(conn, "closed"))
        if keep:
            self._conn = conn
        return conn, keep

    def _drop(self, conn) -> None:
        if self._conn is conn:
            self._conn = None
        try:
            conn.rollback()
        except Exception:
            pass
        try:
            conn.close()
        except Exception:
            pass

    def _using(self, fn):
        with self._lock:
            conn, keep = self._borrow()
            try:
                out = fn(conn)
                conn.commit()
                return out
            except Exception:
                self._drop(conn)
                keep = False
                raise
            finally:
                if not keep:
                    try:
                        conn.close()
                    except Exception:
                        pass

    def token_owner(self, token_sha256: str) -> tuple[str, str] | None:
        def read_owner(conn):
            with conn.cursor() as cur:
                cur.execute(TOKEN_OWNER_SQL, (token_sha256,))
                return cur.fetchone()

        row = self._using(read_owner)
        return (row[0], row[1]) if row else None

    def snapshot(self, tenant: str) -> AclSnapshot:
        def read_snapshot(conn):
            if not hasattr(conn, "pipeline"):
                use_tenant(conn, tenant)
                with conn.cursor() as cur:
                    cur.execute("SELECT person_id, acl_group FROM group_membership WHERE tenant_id = %s", (tenant,))
                    members = cur.fetchall()
                    cur.execute("SELECT acl_group, last_good_sync_at FROM source_sync WHERE tenant_id = %s", (tenant,))
                    syncs = cur.fetchall()
                    cur.execute("SELECT acl_epoch FROM tenant_acl WHERE tenant_id = %s", (tenant,))
                    epoch = cur.fetchone()
                return members, syncs, epoch
            setup, config, members_cur, sync_cur, epoch_cur = (conn.cursor() for _ in range(5))
            try:
                with conn.pipeline():
                    setup.execute(ASSUME_APP_ROLE)
                    config.execute(SET_TENANT, (tenant,))
                    members_cur.execute(
                        "SELECT person_id, acl_group FROM group_membership WHERE tenant_id = %s", (tenant,)
                    )
                    sync_cur.execute(
                        "SELECT acl_group, last_good_sync_at FROM source_sync WHERE tenant_id = %s", (tenant,)
                    )
                    epoch_cur.execute("SELECT acl_epoch FROM tenant_acl WHERE tenant_id = %s", (tenant,))
                config.fetchone()
                return members_cur.fetchall(), sync_cur.fetchall(), epoch_cur.fetchone()
            finally:
                for cur in (setup, config, members_cur, sync_cur, epoch_cur):
                    cur.close()

        members, syncs, epoch = self._using(read_snapshot)

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
