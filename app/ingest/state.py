"""Slack checkpoints and connection nodes.

Checkpoints live in the ingest record store. The connection node stays in
the graph. ``oldest`` stays put until the channel cursor and its thread
cursors are both exhausted.
"""

from datetime import UTC, datetime

from app.ingest.records import SLACK_EVENT, SYNC_FAILURE, SYNC_STATE

_CONNECTION = """
MERGE (t:Tenant {id: $tenant_id})
MERGE (c:Connection {id: $id})
ON CREATE SET
  c.tenant_id = $tenant_id,
  c.connection_id = $connection_id,
  c.source = $source,
  c.workspace_id = $workspace_id,
  c.installed_at = $installed_at
MERGE (t)-[:HAS_CONNECTION]->(c)
"""


def channel_sync_id(tenant_id: str, workspace_id: str, channel_id: str) -> str:
    return f"{tenant_id}:slack:sync:{workspace_id}:{channel_id}"


def thread_sync_id(tenant_id: str, workspace_id: str, channel_id: str, parent_ts: str) -> str:
    return f"{tenant_id}:slack:sync:{workspace_id}:{channel_id}:{parent_ts}"


def failure_id(tenant_id: str, channel_id: str, ts: str) -> str:
    return f"{tenant_id}:slack:failure:{channel_id}:{ts}"


def connection_id_for(tenant_id: str, connection_id: str) -> str:
    return f"{tenant_id}:slack:connection:{connection_id}"


def read_channel_sync(store, sync_id: str) -> dict[str, str]:
    payload = _payload(store, sync_id)
    return {
        "history_cursor": payload.get("history_cursor") or "",
        "oldest": payload.get("oldest") or "0",
        "newest_ts": payload.get("newest_ts") or "",
        "status": payload.get("status") or "",
    }


def write_channel_sync(store, *, sync_id: str, fields: dict[str, str]) -> None:
    store.merge(sync_id, fields.get("tenant_id", ""), SYNC_STATE, {**fields, "updated_at": _now()})


def read_thread_sync(store, sync_id: str) -> dict[str, str]:
    payload = _payload(store, sync_id)
    return {
        "replies_cursor": payload.get("replies_cursor") or "",
        "status": payload.get("status") or "",
    }


def write_thread_sync(store, *, sync_id: str, fields: dict[str, str]) -> None:
    store.merge(sync_id, fields.get("tenant_id", ""), SYNC_STATE, {**fields, "updated_at": _now()})


def write_connection(
    graph,
    *,
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
    source: str = "slack",
) -> None:
    graph.query(
        _CONNECTION,
        {
            "id": f"{tenant_id}:{source}:connection:{connection_id}",
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "source": source,
            "workspace_id": workspace_id,
            "installed_at": _now(),
        },
    )


def failure_attempts(store, node_id: str) -> int:
    payload = _payload(store, node_id)
    attempts = payload.get("attempts")
    if attempts is None:
        return 0
    return int(attempts)


def record_failure(store, *, node_id: str, fields: dict[str, str]) -> int:
    attempts = failure_attempts(store, node_id) + 1
    store.merge(
        node_id,
        fields.get("tenant_id", ""),
        SYNC_FAILURE,
        {**fields, "attempts": attempts, "updated_at": _now()},
    )
    return attempts


def delete_failure(store, node_id: str) -> None:
    store.delete(node_id)


def retryable_failures(store) -> list[tuple]:
    rows = []
    for row in store.list_kind(SYNC_FAILURE):
        if ":slack:failure:" not in row["id"]:
            continue
        payload = row["payload"]
        if int(payload.get("attempts") or 0) >= 5:
            continue
        rows.append(
            (
                row["id"],
                payload.get("tenant_id"),
                payload.get("connection_id"),
                payload.get("workspace_id"),
                payload.get("channel_id"),
                payload.get("message_ts"),
                payload.get("workspace_domain"),
            )
        )
    return rows


def event_already_applied(store, event_id: str) -> bool:
    if not event_id:
        return False
    return store.get(event_id) is not None


def mark_event_applied(store, event_id: str, tenant_id: str) -> None:
    if event_id:
        store.create_once(event_id, tenant_id, SLACK_EVENT, {})


def _payload(store, record_id: str) -> dict:
    row = store.get(record_id)
    if not row:
        return {}
    return row["payload"]


def _now() -> str:
    return datetime.now(UTC).isoformat()
