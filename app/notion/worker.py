"""Poll Notion pages shared with the integration."""

import logging
import time

from app.graph.policy import allowed_source, notion_source_id
from app.graph.upsert import ensure_indexes
from app.ingest.chunks import embed_pending
from app.graph.upsert_document import upsert_document
from app.ingest.log import format_document_log
from app.ingest.records import SYNC_FAILURE, open_store
from app.ingest.state import failure_attempts, write_connection
from app.notion.blocks import plain_text_from_blocks
from app.notion.normalize import normalize_notion_page
from app.notion.time import sortable_timestamp

logger = logging.getLogger("weave.notion")
_MAX_FAILURES = 5


def notion_sync_id(tenant_id: str, workspace_id: str) -> str:
    return f"{tenant_id}:notion:sync:{workspace_id}"


def notion_failure_id(tenant_id: str, page_id: str) -> str:
    return f"{tenant_id}:notion:failure:{page_id}"


def poll_shared_pages(tenant_id: str, connection_id: str, workspace_id: str, client, graph, store=None) -> None:
    found = allowed_source("notion", workspace_id, tenant_id)
    if found is None:
        return
    group, _policy = found
    records = open_store(store)
    ensure_indexes(graph)
    write_connection(
        graph,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=workspace_id,
        source="notion",
    )
    sync_id = notion_sync_id(tenant_id, workspace_id)
    state = _read_sync(records, sync_id)
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
                    _remember(records, tenant_id, connection_id, workspace_id, "", "ValueError")
                    failed = 1
                    break
                edited = raw.get("last_edited_time")
                if watermark and isinstance(edited, str) and sortable_timestamp(edited) <= watermark:
                    reached_watermark = True
                    break
                outcome = _ingest_page(raw, tenant_id, connection_id, workspace_id, client, graph, records)
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
            records,
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
        source = notion_source_id(tenant_id, workspace_id)
        records.note_source_sync(tenant_id, source, group)
        embed_pending(records, tenant_id)
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


def retry_notion_failures(graph, client, store=None, *, tenant_id: str) -> None:
    records = open_store(store)
    rows = []
    for row in records.list_kind(SYNC_FAILURE):
        payload = row["payload"]
        if row["tenant_id"] != tenant_id:
            continue
        if not payload.get("page_id") or int(payload.get("attempts") or 0) >= 5:
            continue
        rows.append(
            (
                row["id"],
                payload.get("tenant_id"),
                payload.get("connection_id"),
                payload.get("workspace_id"),
                payload.get("page_id"),
            )
        )
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
            upsert_document(graph, document, store=records)
        except ValueError:
            _remember(records, tenant_id, connection_id, workspace_id, page_id, "ValueError")
            continue
        records.delete(node_id)


def _ingest_page(raw, tenant_id, connection_id, workspace_id, client, graph, store) -> str:
    page_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
    failure = notion_failure_id(tenant_id, page_id or "missing")
    if failure_attempts(store, failure) >= _MAX_FAILURES:
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
        _remember(store, tenant_id, connection_id, workspace_id, page_id, "ValueError")
        return "blocked"
    if document is None:
        return "skipped"
    upsert_document(graph, document, store=store)
    store.delete(failure)
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


def _remember(store, tenant_id, connection_id, workspace_id, page_id, error_class) -> None:
    from app.ingest.state import record_failure

    record_failure(
        store,
        node_id=notion_failure_id(tenant_id, page_id or "missing"),
        fields={
            "node_id": "",
            "version": "",
            "error_class": error_class,
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "workspace_id": workspace_id,
            "page_id": page_id,
        },
    )


def _read_sync(store, sync_id: str) -> dict[str, str]:
    row = store.get(sync_id)
    payload = row["payload"] if row else {}
    return {
        "search_cursor": payload.get("search_cursor") or "",
        "last_edited_after": payload.get("last_edited_after") or "",
        "status": payload.get("status") or "",
    }


def _write_sync(store, sync_id: str, fields: dict[str, str]) -> None:
    from app.ingest.records import SYNC_STATE

    store.merge(sync_id, fields.get("tenant_id", ""), SYNC_STATE, fields)


def _next_cursor(page: dict) -> str:
    if not page.get("has_more"):
        return ""
    cursor = page.get("next_cursor") or ""
    return cursor if isinstance(cursor, str) else ""


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
