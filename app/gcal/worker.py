"""Poll one Google Calendar."""

import logging
import time

from app.gcal.client import CalendarRequestError
from app.gcal.normalize import normalize_calendar_event
from app.graph.policy import allowed_source, gcal_source_id
from app.graph.upsert import ensure_indexes
from app.graph.upsert_document import upsert_document
from app.ingest.chunks import index_source
from app.ingest.log import format_document_log
from app.ingest.records import SYNC_STATE, open_store
from app.ingest.state import write_connection

logger = logging.getLogger("weave.gcal")


def gcal_sync_id(tenant_id: str, calendar_id: str) -> str:
    return f"{tenant_id}:gcal:sync:{calendar_id}"


def poll_calendar(tenant_id: str, connection_id: str, calendar_id: str, client, graph, store=None) -> None:
    found = allowed_source("gcal", calendar_id)
    if found is None:
        return
    group, _policy = found
    records = open_store(store)
    ensure_indexes(graph)
    write_connection(
        graph,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=calendar_id,
        source="gcal",
    )
    sync_id = gcal_sync_id(tenant_id, calendar_id)
    state = _read(records, sync_id)
    sync_token = state.get("sync_token") or ""
    page_token = state.get("page_token") or ""
    while True:
        started = time.perf_counter()
        try:
            page = client.list_events(sync_token or None, page_token or None)
        except CalendarRequestError as exc:
            if exc.status == 410:
                _write(records, sync_id, _blank_state(tenant_id, connection_id, calendar_id))
                sync_token = ""
                page_token = ""
                continue
            raise
        events = page.get("items") if isinstance(page.get("items"), list) else []
        failed = 0
        for raw in events:
            document = normalize_calendar_event(
                raw,
                tenant_id=tenant_id,
                connection_id=connection_id,
                calendar_id=calendar_id,
            )
            if document is None:
                continue
            if not upsert_document(graph, document).applied:
                failed = 1
                break
        if failed:
            return
        next_page = page.get("nextPageToken") or ""
        next_sync = page.get("nextSyncToken") or sync_token
        finished = next_page == ""
        _write(
            records,
            sync_id,
            {
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "calendar_id": calendar_id,
                "sync_token": next_sync if finished else sync_token,
                "page_token": "" if finished else next_page,
                "status": "complete" if finished else "in_progress",
                "last_error_class": "",
            },
        )
        if finished:
            records.note_source_sync(tenant_id, gcal_source_id(tenant_id, calendar_id), group)
            index_source(graph, records, gcal_source_id(tenant_id, calendar_id))
        logger.info(
            "%s",
            format_document_log(
                tenant_id=tenant_id,
                folder_id=calendar_id,
                records=len(events),
                failed=0,
                duration_ms=_elapsed(started),
                error_class="",
            ),
        )
        if finished:
            return
        page_token = next_page


def _read(records, sync_id: str) -> dict:
    row = records.get(sync_id)
    payload = row["payload"] if row else {}
    return {
        "sync_token": payload.get("sync_token") or "",
        "page_token": payload.get("page_token") or "",
    }


def _write(records, sync_id: str, payload: dict) -> None:
    records.merge(sync_id, payload.get("tenant_id", ""), SYNC_STATE, payload)


def _blank_state(tenant_id: str, connection_id: str, calendar_id: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "connection_id": connection_id,
        "calendar_id": calendar_id,
        "sync_token": "",
        "page_token": "",
        "status": "in_progress",
        "last_error_class": "",
    }


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
