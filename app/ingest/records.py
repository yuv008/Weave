"""Sync checkpoints, failures, and Slack event ids.

FalkorDB no longer stores ``SyncState``, ``SyncFailure``, or ``SlackEvent``.
Tests pass a ``MemoryStore``. A live process uses Postgres.
"""

import json
import os
from datetime import UTC, datetime

SYNC_STATE = "sync_state"
SYNC_FAILURE = "sync_failure"
SLACK_EVENT = "slack_event"


class MemoryStore:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}
        self.changes: list[dict] = []

    def get(self, record_id: str) -> dict | None:
        row = self.rows.get(record_id)
        return None if row is None else {
            "id": row["id"],
            "tenant_id": row["tenant_id"],
            "kind": row["kind"],
            "payload": dict(row["payload"]),
        }

    def merge(self, record_id: str, tenant_id: str, kind: str, payload: dict) -> dict:
        current = self.rows.get(record_id)
        merged = dict(current["payload"]) if current else {}
        merged.update(payload)
        self.rows[record_id] = {
            "id": record_id,
            "tenant_id": tenant_id or (current or {}).get("tenant_id", ""),
            "kind": kind,
            "payload": merged,
            "updated_at": _now(),
        }
        return self.get(record_id)

    def create_once(self, record_id: str, tenant_id: str, kind: str, payload: dict) -> bool:
        if record_id in self.rows:
            return False
        self.merge(record_id, tenant_id, kind, payload)
        return True

    def delete(self, record_id: str) -> None:
        self.rows.pop(record_id, None)

    def list_kind(self, kind: str) -> list[dict]:
        return [self.get(row["id"]) for row in self.rows.values() if row["kind"] == kind]

    def note_source_sync(self, tenant_id: str, source_id: str, acl_group: str) -> None:
        return

    def __init_chunks(self) -> dict:
        chunks = getattr(self, "_chunks", None)
        if chunks is None:
            chunks = {}
            self._chunks = chunks
        return chunks

    def replace_chunks(self, rows: list[dict]) -> None:
        chunks = self.__init_chunks()
        for row in rows:
            chunks[row["id"]] = row

    def delete_chunks(self, item_ids: list[str]) -> None:
        chunks = self.__init_chunks()
        drop = {f"{item_id}:chunk:0" for item_id in item_ids}
        for chunk_id in list(chunks):
            if chunk_id in drop:
                del chunks[chunk_id]

    def record_change(self, tenant_id: str, fact_id: str, kind: str, new_fact_id: str) -> None:
        self.changes.append(
            {"id": f"{fact_id}:{kind}", "tenant_id": tenant_id, "fact_id": fact_id, "kind": kind, "new_fact_id": new_fact_id}
        )


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

    def _connect(self):
        from shared.pg import connect_pg

        default = os.environ.get("DATABASE_URL", "")
        if self.dsn == default:
            return connect_pg()
        return connect_pg(url=self.dsn)

    def get(self, record_id: str) -> dict | None:
        row = self._one(
            "SELECT id, tenant_id, kind, payload FROM ingest_record WHERE id = %s",
            (record_id,),
        )
        if row is None:
            return None
        return {"id": row[0], "tenant_id": row[1], "kind": row[2], "payload": _payload(row[3])}

    def merge(self, record_id: str, tenant_id: str, kind: str, payload: dict) -> dict:
        self._run(
            """
            INSERT INTO ingest_record (id, tenant_id, kind, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO UPDATE SET
              tenant_id = EXCLUDED.tenant_id,
              kind = EXCLUDED.kind,
              payload = ingest_record.payload || EXCLUDED.payload,
              updated_at = now()
            """,
            (record_id, tenant_id, kind, json.dumps(payload)),
            tenant=tenant_id,
        )
        return self.get(record_id)

    def create_once(self, record_id: str, tenant_id: str, kind: str, payload: dict) -> bool:
        row = self._one(
            """
            INSERT INTO ingest_record (id, tenant_id, kind, payload)
            VALUES (%s, %s, %s, %s::jsonb)
            ON CONFLICT (id) DO NOTHING
            RETURNING id
            """,
            (record_id, tenant_id, kind, json.dumps(payload)),
            tenant=tenant_id,
        )
        return row is not None

    def delete(self, record_id: str) -> None:
        self._run("DELETE FROM ingest_record WHERE id = %s", (record_id,))

    def note_source_sync(self, tenant_id: str, source_id: str, acl_group: str) -> None:
        self._run(
            """
            INSERT INTO source_sync (tenant_id, source_id, acl_group, last_good_sync_at)
            VALUES (%s, %s, %s, now())
            ON CONFLICT (tenant_id, source_id) DO UPDATE SET
              acl_group = EXCLUDED.acl_group,
              last_good_sync_at = now()
            """,
            (tenant_id, source_id, acl_group),
        )

    def replace_chunks(self, rows: list[dict]) -> None:
        if not rows:
            return
        from fixtures.seed import vector_literal

        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO chunk (id, tenant_id, item_id, ordinal, text, embedding, acl_groups, min_level)
                    VALUES (%s, %s, %s, 0, %s, %s::vector, %s, %s)
                    ON CONFLICT (id) DO UPDATE SET
                      text = EXCLUDED.text,
                      embedding = COALESCE(EXCLUDED.embedding, chunk.embedding),
                      acl_groups = EXCLUDED.acl_groups,
                      min_level = EXCLUDED.min_level
                    """,
                    [
                        (
                            row["id"],
                            row["tenant_id"],
                            row["item_id"],
                            row["text"],
                            vector_literal(row.get("embedding")),
                            row["acl_groups"],
                            row["min_level"],
                        )
                        for row in rows
                    ],
                )
            conn.commit()
        finally:
            conn.close()

    def delete_chunks(self, item_ids: list[str]) -> None:
        if not item_ids:
            return
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM chunk WHERE item_id = ANY(%s)", (item_ids,))
            conn.commit()
        finally:
            conn.close()

    def record_change(self, tenant_id: str, fact_id: str, kind: str, new_fact_id: str) -> None:
        self._run(
            """
            INSERT INTO change_event (id, tenant_id, fact_id, kind, new_fact_id)
            VALUES (%s, %s, %s, %s, %s)
            ON CONFLICT (id) DO NOTHING
            """,
            (f"{fact_id}:{kind}", tenant_id, fact_id, kind, new_fact_id),
        )

    def list_kind(self, kind: str) -> list[dict]:
        conn = self._connect()
        try:
            cur = conn.cursor()
            cur.execute(
                "SELECT id, tenant_id, kind, payload FROM ingest_record WHERE kind = %s",
                (kind,),
            )
            rows = cur.fetchall()
            conn.commit()
        finally:
            conn.close()
        return [
            {"id": row[0], "tenant_id": row[1], "kind": row[2], "payload": _payload(row[3])}
            for row in rows
        ]

    def _run(self, sql: str, params: tuple, *, tenant: str | None = None) -> None:
        from shared.pg import use_tenant

        conn = self._connect()
        try:
            use_tenant(conn, tenant)
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _one(self, sql: str, params: tuple, *, tenant: str | None = None):
        from shared.pg import use_tenant

        conn = self._connect()
        try:
            use_tenant(conn, tenant)
            cur = conn.cursor()
            cur.execute(sql, params)
            row = cur.fetchone()
            conn.commit()
            return row
        finally:
            conn.close()


def postgres_store() -> PostgresStore:
    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        raise RuntimeError("DATABASE_URL is required for ingest records")
    return PostgresStore(dsn)


def open_store(store):
    return store if store is not None else postgres_store()


def _payload(value) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    return dict(value or {})


def _now() -> str:
    return datetime.now(UTC).isoformat()
