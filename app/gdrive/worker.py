"""Poll Google Docs in one shared folder."""

import logging
import time
from datetime import UTC, datetime

from app.gdrive.client import DOC_MIME
from app.gdrive.normalize import normalize_drive_file
from app.graph.upsert import ensure_indexes
from app.graph.upsert_document import upsert_document
from app.ingest.log import format_document_log
from app.ingest.state import failure_attempts, write_connection
from app.notion.time import sortable_timestamp

logger = logging.getLogger("weave.gdrive")
_MAX_FAILURES = 5

_READ = """
MATCH (s:SyncState {id: $id})
RETURN s.page_token, s.modified_after, s.status
"""

_WRITE = """
MERGE (s:SyncState {id: $id})
SET s.tenant_id = $tenant_id,
    s.connection_id = $connection_id,
    s.folder_id = $folder_id,
    s.workspace_id = $folder_id,
    s.source = 'gdrive',
    s.page_token = $page_token,
    s.modified_after = $modified_after,
    s.status = $status,
    s.last_error_class = $last_error_class,
    s.updated_at = $updated_at
"""

_FAILURE = """
MERGE (f:SyncFailure {id: $id})
ON CREATE SET f.attempts = 0
SET f.attempts = coalesce(f.attempts, 0) + 1,
    f.error_class = $error_class,
    f.tenant_id = $tenant_id,
    f.connection_id = $connection_id,
    f.folder_id = $folder_id,
    f.file_id = $file_id,
    f.updated_at = $updated_at
RETURN f.attempts
"""


def drive_sync_id(tenant_id: str, folder_id: str) -> str:
    return f"{tenant_id}:gdrive:sync:{folder_id}"


def drive_failure_id(tenant_id: str, file_id: str) -> str:
    return f"{tenant_id}:gdrive:failure:{file_id}"


def poll_folder(tenant_id: str, connection_id: str, folder_id: str, client, graph) -> None:
    ensure_indexes(graph)
    write_connection(
        graph,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=folder_id,
        source="gdrive",
    )
    sync_id = drive_sync_id(tenant_id, folder_id)
    state = _read(graph, sync_id)
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
                outcome = _ingest(raw, tenant_id, connection_id, folder_id, client, graph)
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
            graph,
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


def retry_drive_failures(graph, client) -> None:
    rows = graph.query(
        """
        MATCH (f:SyncFailure)
        WHERE f.file_id IS NOT NULL AND coalesce(f.attempts, 0) < 5
        RETURN f.id, f.tenant_id, f.connection_id, f.folder_id, f.file_id
        """
    ).result_set
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
            upsert_document(graph, document)
        except ValueError:
            _remember(graph, tenant_id, connection_id, folder_id, file_id, "ValueError")
            continue
        graph.query("MATCH (f:SyncFailure {id: $id}) DELETE f", {"id": node_id})


def _ingest(raw, tenant_id, connection_id, folder_id, client, graph) -> str:
    file_id = raw.get("id") if isinstance(raw.get("id"), str) else ""
    failure = drive_failure_id(tenant_id, file_id or "missing")
    if failure_attempts(graph, failure) >= _MAX_FAILURES:
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
        _remember(graph, tenant_id, connection_id, folder_id, file_id, "ValueError")
        return "blocked"
    if document is None:
        return "skipped"
    upsert_document(graph, document)
    graph.query("MATCH (f:SyncFailure {id: $id}) DELETE f", {"id": failure})
    return "ok"


def _remember(graph, tenant_id, connection_id, folder_id, file_id, error_class) -> None:
    graph.query(
        _FAILURE,
        {
            "id": drive_failure_id(tenant_id, file_id or "missing"),
            "error_class": error_class,
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "folder_id": folder_id,
            "file_id": file_id,
            "updated_at": datetime.now(UTC).isoformat(),
        },
    )


def _read(graph, sync_id: str) -> dict[str, str]:
    rows = graph.query(_READ, {"id": sync_id}).result_set
    if not rows:
        return {"page_token": "", "modified_after": "", "status": ""}
    token, watermark, status = rows[0]
    return {"page_token": token or "", "modified_after": watermark or "", "status": status or ""}


def _write(graph, sync_id: str, fields: dict[str, str]) -> None:
    graph.query(_WRITE, {"id": sync_id, **fields, "updated_at": datetime.now(UTC).isoformat()})


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
