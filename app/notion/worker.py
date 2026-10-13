"""Poll Notion pages shared with the integration."""

import logging
import time

from app.graph.upsert import ensure_indexes
from app.graph.upsert_document import upsert_document
from app.ingest.log import format_document_log
from app.ingest.state import failure_attempts, write_connection
from app.notion.blocks import plain_text_from_blocks
from app.notion.normalize import normalize_notion_page
from app.notion.time import sortable_timestamp

logger = logging.getLogger("weave.notion")
_MAX_FAILURES = 5

_READ_SYNC = """
MATCH (s:SyncState {id: $id})
RETURN s.search_cursor, s.last_edited_after, s.status
"""

_WRITE_SYNC = """
MERGE (s:SyncState {id: $id})
SET s.tenant_id = $tenant_id,
    s.connection_id = $connection_id,
    s.workspace_id = $workspace_id,
    s.source = 'notion',
    s.search_cursor = $search_cursor,
    s.last_edited_after = $last_edited_after,
    s.status = $status,
    s.last_error_class = $last_error_class,
    s.updated_at = $updated_at
"""

_FAILURE = """
MERGE (f:SyncFailure {id: $id})
ON CREATE SET f.attempts = 0
SET f.attempts = coalesce(f.attempts, 0) + 1,
    f.node_id = $node_id,
    f.version = $version,
    f.error_class = $error_class,
    f.tenant_id = $tenant_id,
    f.connection_id = $connection_id,
    f.workspace_id = $workspace_id,
    f.page_id = $page_id,
    f.updated_at = $updated_at
RETURN f.attempts
"""


def notion_sync_id(tenant_id: str, workspace_id: str) -> str:
    return f"{tenant_id}:notion:sync:{workspace_id}"


def notion_failure_id(tenant_id: str, page_id: str) -> str:
    return f"{tenant_id}:notion:failure:{page_id}"


def poll_shared_pages(tenant_id: str, connection_id: str, workspace_id: str, client, graph) -> None:
    ensure_indexes(graph)
    write_connection(
        graph,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=workspace_id,
        source="notion",
    )
    sync_id = notion_sync_id(tenant_id, workspace_id)
    state = _read_sync(graph, sync_id)
    cursor = state["search_cursor"]
    watermark = state["last_edited_after"]
    greatest = watermark
    while True:
        started = time.perf_counter()
        page = client.search_pages(cursor or None, watermark or None)
        results = page.get("results") or []
        failed = 0
        reached_watermark = False
        try:
            for raw in results:
                if not isinstance(raw, dict):
                    _remember(graph, tenant_id, connection_id, workspace_id, "", "ValueError")
                    failed = 1
                    break
                edited = raw.get("last_edited_time")
                if watermark and isinstance(edited, str) and sortable_timestamp(edited) <= watermark:
                    reached_watermark = True
                    break
                outcome = _ingest_page(raw, tenant_id, connection_id, workspace_id, client, graph)
                if outcome == "blocked":
                    failed = 1
                    break
                if isinstance(edited, str):
                    stamped = sortable_timestamp(edited)
                    if not greatest or stamped > greatest:
                        greatest = stamped
        except Exception as exc:
            logger.info(
                "%s",
                format_document_log(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    records=len(results),
                    failed=1,
                    duration_ms=_elapsed(started),
                    error_class=type(exc).__name__,
                ),
            )
            raise
        if failed:
            logger.info(
                "%s",
                format_document_log(
                    tenant_id=tenant_id,
                    workspace_id=workspace_id,
                    records=len(results),
                    failed=1,
                    duration_ms=_elapsed(started),
                    error_class="ValueError",
                ),
            )
            return
        next_cursor = "" if reached_watermark else _next_cursor(page)
        finished = next_cursor == ""
        _write_sync(
            graph,
            sync_id,
            {
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "workspace_id": workspace_id,
                "search_cursor": "" if finished else next_cursor,
                "last_edited_after": greatest if finished and greatest else watermark,
                "status": "complete" if finished else "in_progress",
                "last_error_class": "",
            },
        )
        logger.info(
            "%s",
            format_document_log(
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                records=len(results),
                failed=0,
                duration_ms=_elapsed(started),
                error_class="",
            ),
        )
        if finished:
            return
        cursor = next_cursor


def retry_notion_failures(graph, client) -> None:
    rows = graph.query(
        """
        MATCH (f:SyncFailure)
        WHERE f.page_id IS NOT NULL AND coalesce(f.attempts, 0) < 5
        RETURN f.id, f.tenant_id, f.connection_id, f.workspace_id, f.page_id
        """
    ).result_set
    for node_id, tenant_id, connection_id, workspace_id, page_id in rows:
        try:
            page = client.retrieve_page(page_id)
            text = "" if page.get("in_trash") or page.get("archived") else _page_text(client, page_id)
            document = normalize_notion_page(
                page,
                text,
                tenant_id=tenant_id,
                connection_id=connection_id,
                workspace_id=workspace_id,
            )
            if document is None:
                raise ValueError("not a page")
            upsert_document(graph, document)
        except ValueError:
            _remember(graph, tenant_id, connection_id, workspace_id, page_id, "ValueError")
            continue
        graph.query("MATCH (f:SyncFailure {id: $id}) DELETE f", {"id": node_id})


def _ingest_page(raw, tenant_id, connection_id, workspace_id, client, graph) -> str:
    page_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
    failure = notion_failure_id(tenant_id, page_id or "missing")
    if failure_attempts(graph, failure) >= _MAX_FAILURES:
        return "skipped"
    try:
        page = client.retrieve_page(page_id) if page_id else raw
        deleted = bool(page.get("in_trash") or page.get("archived"))
        text = "" if deleted or not page_id else _page_text(client, page_id)
        document = normalize_notion_page(
            page,
            text,
            tenant_id=tenant_id,
            connection_id=connection_id,
            workspace_id=workspace_id,
        )
    except ValueError:
        _remember(graph, tenant_id, connection_id, workspace_id, page_id, "ValueError")
        return "blocked"
    if document is None:
        return "skipped"
    upsert_document(graph, document)
    graph.query("MATCH (f:SyncFailure {id: $id}) DELETE f", {"id": failure})
    return "ok"


def _page_text(client, page_id: str) -> str:
    blocks = []
    cursor = None
    while True:
        payload = client.block_children(page_id, cursor)
        blocks.extend(payload.get("results") or [])
        cursor = _next_cursor(payload)
        if not cursor:
            break
    return plain_text_from_blocks(blocks)


def _remember(graph, tenant_id, connection_id, workspace_id, page_id, error_class) -> None:
    from datetime import UTC, datetime

    graph.query(
        _FAILURE,
        {
            "id": notion_failure_id(tenant_id, page_id or "missing"),
            "node_id": "",
            "version": "",
            "error_class": error_class,
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "workspace_id": workspace_id,
            "page_id": page_id,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def _read_sync(graph, sync_id: str) -> dict[str, str]:
    rows = graph.query(_READ_SYNC, {"id": sync_id}).result_set
    if not rows:
        return {"search_cursor": "", "last_edited_after": "", "status": ""}
    cursor, watermark, status = rows[0]
    return {
        "search_cursor": cursor or "",
        "last_edited_after": watermark or "",
        "status": status or "",
    }


def _write_sync(graph, sync_id: str, fields: dict[str, str]) -> None:
    from datetime import UTC, datetime

    graph.query(_WRITE_SYNC, {"id": sync_id, **fields, "updated_at": datetime.now(UTC).isoformat()})


def _next_cursor(page: dict) -> str:
    if not page.get("has_more"):
        return ""
    cursor = page.get("next_cursor") or ""
    return cursor if isinstance(cursor, str) else ""


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
