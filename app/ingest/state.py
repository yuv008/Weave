"""Temporary FalkorDB checkpoints.

``newest_ts`` remembers the greatest message ts seen on pages that have
already been written. ``oldest`` stays put until the channel cursor and its
thread cursors are both exhausted. A relational database can replace this
node later.
"""

from datetime import UTC, datetime

_CHANNEL_READ = """
MATCH (s:SyncState {id: $id})
RETURN s.history_cursor, s.oldest, s.newest_ts, s.status
"""

_THREAD_READ = """
MATCH (s:SyncState {id: $id})
RETURN s.replies_cursor, s.status
"""

_CHANNEL_WRITE = """
MERGE (s:SyncState {id: $id})
SET s.tenant_id = $tenant_id,
    s.connection_id = $connection_id,
    s.workspace_id = $workspace_id,
    s.channel_id = $channel_id,
    s.history_cursor = $history_cursor,
    s.oldest = $oldest,
    s.newest_ts = $newest_ts,
    s.status = $status,
    s.last_error_class = $last_error_class,
    s.updated_at = $updated_at
"""

_THREAD_WRITE = """
MERGE (s:SyncState {id: $id})
SET s.tenant_id = $tenant_id,
    s.connection_id = $connection_id,
    s.workspace_id = $workspace_id,
    s.channel_id = $channel_id,
    s.parent_ts = $parent_ts,
    s.replies_cursor = $replies_cursor,
    s.status = $status,
    s.updated_at = $updated_at
"""

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

_FAILURE = """
MERGE (f:SyncFailure {id: $id})
ON CREATE SET f.attempts = 0
SET f.attempts = coalesce(f.attempts, 0) + 1,
    f.node_id = $node_id,
    f.version = $version,
    f.error_class = $error_class,
    f.updated_at = $updated_at,
    f.tenant_id = $tenant_id,
    f.connection_id = $connection_id,
    f.workspace_id = $workspace_id,
    f.channel_id = $channel_id,
    f.message_ts = $message_ts,
    f.workspace_domain = $workspace_domain
RETURN f.attempts
"""

_ATTEMPTS = """
MATCH (f:SyncFailure {id: $id})
RETURN f.attempts
"""

_DELETE_FAILURE = """
MATCH (f:SyncFailure {id: $id})
DELETE f
"""

_RETRYABLE = """
MATCH (f:SyncFailure)
WHERE coalesce(f.attempts, 0) < 5
RETURN f.id, f.tenant_id, f.connection_id, f.workspace_id, f.channel_id,
       f.message_ts, f.workspace_domain
"""

_EVENT_MATCH = """
MATCH (e:SlackEvent {id: $id})
RETURN e.id
"""

_EVENT_CREATE = """
CREATE (:SlackEvent {id: $id, tenant_id: $tenant_id})
"""


def channel_sync_id(tenant_id: str, workspace_id: str, channel_id: str) -> str:
    return f"{tenant_id}:slack:sync:{workspace_id}:{channel_id}"


def thread_sync_id(tenant_id: str, workspace_id: str, channel_id: str, parent_ts: str) -> str:
    return f"{tenant_id}:slack:sync:{workspace_id}:{channel_id}:{parent_ts}"


def failure_id(tenant_id: str, channel_id: str, ts: str) -> str:
    return f"{tenant_id}:slack:failure:{channel_id}:{ts}"


def connection_id_for(tenant_id: str, connection_id: str) -> str:
    return f"{tenant_id}:slack:connection:{connection_id}"


def read_channel_sync(graph, sync_id: str) -> dict[str, str]:
    rows = graph.query(_CHANNEL_READ, {"id": sync_id}).result_set
    if not rows:
        return {"history_cursor": "", "oldest": "0", "newest_ts": "", "status": ""}
    cursor, oldest, newest, status = rows[0]
    return {
        "history_cursor": cursor or "",
        "oldest": oldest or "0",
        "newest_ts": newest or "",
        "status": status or "",
    }


def write_channel_sync(graph, *, sync_id: str, fields: dict[str, str]) -> None:
    graph.query(_CHANNEL_WRITE, {"id": sync_id, **fields, "updated_at": _now()})


def read_thread_sync(graph, sync_id: str) -> dict[str, str]:
    rows = graph.query(_THREAD_READ, {"id": sync_id}).result_set
    if not rows:
        return {"replies_cursor": "", "status": ""}
    cursor, status = rows[0]
    return {"replies_cursor": cursor or "", "status": status or ""}


def write_thread_sync(graph, *, sync_id: str, fields: dict[str, str]) -> None:
    graph.query(_THREAD_WRITE, {"id": sync_id, **fields, "updated_at": _now()})


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


def failure_attempts(graph, node_id: str) -> int:
    rows = graph.query(_ATTEMPTS, {"id": node_id}).result_set
    if not rows or rows[0][0] is None:
        return 0
    return int(rows[0][0])


def record_failure(graph, *, node_id: str, fields: dict[str, str]) -> int:
    rows = graph.query(_FAILURE, {"id": node_id, **fields, "updated_at": _now()}).result_set
    return int(rows[0][0])


def delete_failure(graph, node_id: str) -> None:
    graph.query(_DELETE_FAILURE, {"id": node_id})


def retryable_failures(graph) -> list[tuple]:
    return list(graph.query(_RETRYABLE).result_set)


def event_already_applied(graph, event_id: str) -> bool:
    if not event_id:
        return False
    return bool(graph.query(_EVENT_MATCH, {"id": event_id}).result_set)


def mark_event_applied(graph, event_id: str, tenant_id: str) -> None:
    if event_id:
        graph.query(_EVENT_CREATE, {"id": event_id, "tenant_id": tenant_id})


def _now() -> str:
    return datetime.now(UTC).isoformat()
