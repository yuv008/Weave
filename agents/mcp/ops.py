"""GET /ops: numbers for one tenant, from the owner connection.

Auth is ``Authorization: Bearer <OPS_TOKEN>``. When ``OPS_TOKEN`` is unset the
route is 404, so a demo that has no ops token does not expose the page. The
tenant is ``WEAVE_TENANT`` only. Aggregates use ``connect_pg`` (the owner
connection), not an RLS helper. The page is counts, ages, and a latency
percentile: no payloads, quotes, or message text.
"""

import hashlib
import html
import logging
import os
import secrets
from decimal import Decimal

import anyio
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, PlainTextResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from shared.pg import connect_pg

log = logging.getLogger("weave.ops")

# Still open, so they count toward queue depth:
#   proposed — waiting on a person (agents/tools/drafts.py, agents/answer/actions.py)
#   ready    — waiting on a worker (agents/watcher/watcher.py, agents/answer/actions.py)
#   claimed  — a worker holds it (agents/memory/store.py claim_task)
# Terminal, so they are excluded:
#   accepted, rejected — the person confirmed (agents/answer/actions.py)
#   expired, failed    — handoff or sender stopped (agents/watcher/handoff.py, sender.py)
#   done               — handoff finished (agents/watcher/handoff.py)
#   sent               — DM delivered (agents/watcher/sender.py)
_TERMINAL_STATES = ("accepted", "done", "expired", "failed", "rejected", "sent")
_TERMINAL_LIST = ", ".join(f"'{state}'" for state in _TERMINAL_STATES)

_SYNC_SQL = """
SELECT source_id, acl_group,
       floor(extract(epoch FROM (now() - last_good_sync_at)))::bigint
FROM source_sync
WHERE tenant_id = %s
ORDER BY source_id, acl_group
"""

_LAG_SQL = """
SELECT kind,
       floor(extract(epoch FROM (now() - max(updated_at))))::bigint
FROM ingest_record
WHERE tenant_id = %s
GROUP BY kind
ORDER BY kind
"""

_FAILURE_SQL = """
SELECT count(*)::bigint
FROM ingest_record
WHERE tenant_id = %s AND kind = %s
"""

_EVENT_SQL = """
SELECT count(*)::bigint
FROM change_event
WHERE tenant_id = %s AND NOT processed
"""

_TASK_SQL = f"""
SELECT count(*)::bigint
FROM tasks
WHERE tenant_id = %s
  AND state NOT IN ({_TERMINAL_LIST})
"""

# audit_log.ms is int. percentile_cont requires a continuous type.
_P95_SQL = """
SELECT percentile_cont(0.95) WITHIN GROUP (ORDER BY ms::double precision)
FROM audit_log
WHERE tenant_id = %s
  AND at >= now() - interval '15 minutes'
"""

_NO_STORE = {"cache-control": "no-store"}


def _ops_token() -> str:
    return os.environ.get("OPS_TOKEN", "").strip()


def _tenant() -> str:
    return os.environ.get("WEAVE_TENANT", "").strip()


def _bearer(request: Request) -> str | None:
    header = request.headers.get("authorization")
    if not header:
        return None
    scheme, _, token = header.partition(" ")
    token = token.strip()
    if scheme.lower() != "bearer" or not token:
        return None
    return token


def _same_token(presented: str, expected: str) -> bool:
    return secrets.compare_digest(
        hashlib.sha256(presented.encode("utf-8")).digest(),
        hashlib.sha256(expected.encode("utf-8")).digest(),
    )


def _wants_html(request: Request) -> bool:
    if request.query_params.get("format", "").lower() == "html":
        return True
    accept = request.headers.get("accept", "")
    for part in accept.split(","):
        if part.split(";", 1)[0].strip().lower() == "text/html":
            return True
    return False


def _whole(value) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(type(value).__name__)
    if isinstance(value, int):
        return value
    return int(value)


def _number(value):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float, Decimal)):
        raise TypeError(type(value).__name__)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value) if value.is_integer() else value
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _rows(cur, sql: str, params: tuple):
    cur.execute(sql, params)
    return cur.fetchall()


def _count(cur, sql: str, params: tuple) -> int:
    cur.execute(sql, params)
    row = cur.fetchone()
    if row is None or row[0] is None:
        return 0
    return _whole(row[0])


def _scalar(cur, sql: str, params: tuple):
    cur.execute(sql, params)
    row = cur.fetchone()
    if row is None:
        return None
    return _number(row[0])


def _failure_kind() -> str:
    from app.ingest.records import SYNC_FAILURE

    return SYNC_FAILURE


def read_ops(tenant: str) -> dict:
    """Read-only aggregates for ``tenant``. Caller must pass ``WEAVE_TENANT``."""

    tenant = tenant.strip()
    if not tenant:
        raise ValueError("tenant is required")
    failure_kind = _failure_kind()
    conn = connect_pg()
    try:
        cur = conn.cursor()
        try:
            cur.execute("SET TRANSACTION READ ONLY")
            sources = _rows(cur, _SYNC_SQL, (tenant,))
            lag = _rows(cur, _LAG_SQL, (tenant,))
            failures = _count(cur, _FAILURE_SQL, (tenant, failure_kind))
            events = _count(cur, _EVENT_SQL, (tenant,))
            tasks = _count(cur, _TASK_SQL, (tenant,))
            p95 = _scalar(cur, _P95_SQL, (tenant,))
        finally:
            cur.close()
    finally:
        try:
            conn.rollback()
        finally:
            conn.close()
    return {
        "permission_sync": sorted(
            (
                {"source_id": source_id, "acl_group": acl_group, "age_seconds": _whole(age)}
                for source_id, acl_group, age in sources
            ),
            key=lambda row: (row["source_id"], row["acl_group"]),
        ),
        "ingestion": {
            "lag_seconds": sorted(
                ({"kind": kind, "age_seconds": _whole(age)} for kind, age in lag),
                key=lambda row: row["kind"],
            ),
            "sync_failures": failures,
        },
        "queue": {
            "change_events_unprocessed": events,
            "tasks_open": tasks,
        },
        "p95_ms": p95,
    }


def _show(value) -> str:
    if value is None:
        return ""
    return str(value)


def render_html(snapshot: dict) -> str:
    """One plain page. Identifiers label the numbers; no quotes or message text."""

    lines = ["permission_sync_age_seconds"]
    for row in snapshot["permission_sync"]:
        lines.append(f"{row['source_id']} {row['acl_group']} {_show(row['age_seconds'])}")
    lines.append("ingestion_lag_seconds")
    for row in snapshot["ingestion"]["lag_seconds"]:
        lines.append(f"{row['kind']} {_show(row['age_seconds'])}")
    ingestion = snapshot["ingestion"]
    queue = snapshot["queue"]
    lines.append(f"sync_failures {_show(ingestion['sync_failures'])}")
    lines.append(f"change_events_unprocessed {_show(queue['change_events_unprocessed'])}")
    lines.append(f"tasks_open {_show(queue['tasks_open'])}")
    p95 = snapshot["p95_ms"]
    lines.append("p95_ms" if p95 is None else f"p95_ms {_show(p95)}")
    body = html.escape("\n".join(lines), quote=True)
    return (
        "<!DOCTYPE html>\n"
        "<html><head><meta charset=\"utf-8\"><title>ops</title></head>\n"
        f"<body><pre>{body}</pre></body></html>\n"
    )


async def ops_response(request: Request) -> Response:
    expected = _ops_token()
    if not expected:
        return PlainTextResponse("not found", status_code=404)
    presented = _bearer(request)
    if presented is None or not _same_token(presented, expected):
        return PlainTextResponse(
            "not authorized", status_code=401, headers={"www-authenticate": "Bearer"}
        )
    if request.method != "GET":
        return PlainTextResponse("method not allowed", status_code=405, headers={"allow": "GET"})
    tenant = _tenant()
    if not tenant:
        return PlainTextResponse("unavailable", status_code=503)
    try:
        snapshot = await anyio.to_thread.run_sync(lambda: read_ops(tenant))
    except Exception as exc:
        log.error("ops read failed (%s)", type(exc).__name__)
        return PlainTextResponse("unavailable", status_code=503)
    if _wants_html(request):
        return HTMLResponse(render_html(snapshot), headers=_NO_STORE)
    return JSONResponse(snapshot, headers=_NO_STORE)


class OpsFront:
    """``GET /ops`` in front of user-token auth. Ops uses ``OPS_TOKEN`` only."""

    def __init__(self, app: ASGIApp):
        self._app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        path = scope.get("path", "")
        if scope["type"] == "http" and path in {"/ops", "/ops/"}:
            response = await ops_response(Request(scope, receive))
            await response(scope, receive, send)
            return
        await self._app(scope, receive, send)
