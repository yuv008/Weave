"""Poll Google Docs in one shared folder."""

import logging
import time
from datetime import UTC, datetime

from app.gdrive.client import DOC_MIME
from app.gdrive.normalize import normalize_drive_file
from app.graph.policy import allowed_source, drive_source_id
from app.graph.upsert import ensure_indexes
from app.ingest.chunks import embed_pending
from app.graph.upsert_document import upsert_document
from app.ingest.log import format_document_log
from app.ingest.records import SYNC_FAILURE, SYNC_STATE, open_store
from app.ingest.state import failure_attempts, record_failure, write_connection
from app.notion.time import sortable_timestamp

logger = logging.getLogger("weave.gdrive")
_MAX_FAILURES = 5


def drive_sync_id(tenant_id: str, folder_id: str) -> str:
    return f"{tenant_id}:gdrive:sync:{folder_id}"


def drive_failure_id(tenant_id: str, file_id: str) -> str:
    return f"{tenant_id}:gdrive:failure:{file_id}"


def poll_folder(tenant_id: str, connection_id: str, folder_id: str, client, graph, store=None) -> None:
    found = allowed_source("gdrive", folder_id, tenant_id)
    if found is None:
        return
    group, _policy = found
    records = open_store(store)
    ensure_indexes(graph)
    write_connection(
        graph,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=folder_id,
        source="gdrive",
    )
    sync_id = drive_sync_id(tenant_id, folder_id)
    state = _read(records, sync_id)
    token = state["page_token"]
    watermark = state["modified_after"]
    greatest = watermark
    while True:
        started = time.perf_counter()
        page = client.list_files(folder_id, token or None, watermark or None)
        files = page.get("files") or []
        failed = 0
        try:
            for raw in files:
                if not isinstance(raw, dict) or raw.get("mimeType") != DOC_MIME:
                    continue
                modified = raw.get("modifiedTime")
                outcome = _ingest(raw, tenant_id, connection_id, folder_id, client, graph, records)
                if outcome == "blocked":
                    failed = 1
                    break
                if isinstance(modified, str):
                    stamped = sortable_timestamp(modified)
                    if not greatest or stamped > greatest:
                        greatest = stamped
        except Exception as exc:
            logger.info(
                "%s",
                format_document_log(
                    tenant_id=tenant_id,
                    folder_id=folder_id,
                    records=len(files),
                    failed=1,
                    duration_ms=_elapsed(started),
                    error_class=type(exc).__name__,
                ),
            )
            raise
        if failed:
            return
        next_token = page.get("nextPageToken") or ""
        if not isinstance(next_token, str):
            next_token = ""
        finished = next_token == ""
        _write(
            records,
            sync_id,
            {
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "folder_id": folder_id,
                "page_token": "" if finished else next_token,
                "modified_after": greatest if finished and greatest else watermark,
                "status": "complete" if finished else "in_progress",
                "last_error_class": "",
            },
        )
        source = drive_source_id(tenant_id, folder_id)
        records.note_source_sync(tenant_id, source, group)
        embed_pending(records, tenant_id)
        logger.info(
            "%s",
            format_document_log(
                tenant_id=tenant_id,
                folder_id=folder_id,
                records=len(files),
                failed=0,
                duration_ms=_elapsed(started),
                error_class="",
            ),
        )
        if finished:
            return
        token = next_token


def retry_drive_failures(graph, client, store=None, *, tenant_id: str) -> None:
    records = open_store(store)
    rows = []
    for row in records.list_kind(SYNC_FAILURE):
        payload = row["payload"]
        if row["tenant_id"] != tenant_id:
            continue
        if not payload.get("file_id") or int(payload.get("attempts") or 0) >= 5:
            continue
        rows.append(
            (
                row["id"],
                payload.get("tenant_id"),
                payload.get("connection_id"),
                payload.get("folder_id"),
                payload.get("file_id"),
            )
        )
    for node_id, tenant_id, connection_id, folder_id, file_id in rows:
        try:
            metadata = client.get_file(file_id)
            text = "" if metadata.get("trashed") or metadata.get("mimeType") != DOC_MIME else client.export_document(file_id)
            document = normalize_drive_file(
                metadata,
                text,
                tenant_id=tenant_id,
                connection_id=connection_id,
                folder_id=folder_id,
            )
            if document is None:
                raise ValueError("not a document")
            upsert_document(graph, document, store=records)
        except ValueError:
            _remember(records, tenant_id, connection_id, folder_id, file_id, "ValueError")
            continue
        records.delete(node_id)


def _ingest(raw, tenant_id, connection_id, folder_id, client, graph, store) -> str:
    file_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
    failure = drive_failure_id(tenant_id, file_id or "missing")
    if failure_attempts(store, failure) >= _MAX_FAILURES:
        return "skipped"
    try:
        text = "" if raw.get("trashed") else client.export_document(file_id)
        document = normalize_drive_file(
            raw,
            text,
            tenant_id=tenant_id,
            connection_id=connection_id,
            folder_id=folder_id,
        )
    except ValueError:
        _remember(store, tenant_id, connection_id, folder_id, file_id, "ValueError")
        return "blocked"
    if document is None:
        return "skipped"
    upsert_document(graph, document, store=store)
    store.delete(failure)
    return "ok"


def _remember(store, tenant_id, connection_id, folder_id, file_id, error_class) -> None:
    record_failure(
        store,
        node_id=drive_failure_id(tenant_id, file_id or "missing"),
        fields={
            "error_class": error_class,
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "folder_id": folder_id,
            "file_id": file_id,
        },
    )


def _read(store, sync_id: str) -> dict[str, str]:
    row = store.get(sync_id)
    payload = row["payload"] if row else {}
    return {
        "page_token": payload.get("page_token") or "",
        "modified_after": payload.get("modified_after") or "",
        "status": payload.get("status") or "",
    }


def _write(store, sync_id: str, fields: dict[str, str]) -> None:
    store.merge(sync_id, fields.get("tenant_id", ""), SYNC_STATE, fields)


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
