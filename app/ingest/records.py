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


class PostgresStore:
    def __init__(self, dsn: str) -> None:
        self.dsn = dsn

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
        )
        return row is not None

    def delete(self, record_id: str) -> None:
        self._run("DELETE FROM ingest_record WHERE id = %s", (record_id,))

    def list_kind(self, kind: str) -> list[dict]:
        from shared.pg import connect_pg

        conn = connect_pg(url=self.dsn)
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

    def _run(self, sql: str, params: tuple) -> None:
        from shared.pg import connect_pg

        conn = connect_pg(url=self.dsn)
        try:
            cur = conn.cursor()
            cur.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def _one(self, sql: str, params: tuple):
        from shared.pg import connect_pg

        conn = connect_pg(url=self.dsn)
        try:
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
