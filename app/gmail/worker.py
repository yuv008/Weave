"""Poll one Gmail mailbox."""

import logging
import time

from app.gmail.client import GmailRequestError
from app.gmail.normalize import normalize_gmail_message
from app.graph.policy import allowed_source, gmail_source_id
from app.graph.upsert import ensure_indexes
from app.graph.upsert_gmail import upsert_gmail_message
from app.ingest.chunks import index_source
from app.ingest.log import format_document_log
from app.ingest.records import SYNC_STATE, open_store
from app.ingest.state import write_connection

logger = logging.getLogger("weave.gmail")


def gmail_sync_id(tenant_id: str, mailbox: str) -> str:
    return f"{tenant_id}:gmail:sync:{mailbox}"


def poll_mailbox(tenant_id: str, connection_id: str, mailbox: str, client, graph, store=None) -> None:
    found = allowed_source("gmail", mailbox, tenant_id)
    if found is None:
        return
    group, _policy = found
    records = open_store(store)
    ensure_indexes(graph)
    write_connection(
        graph,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=mailbox,
        source="gmail",
    )
    sync_id = gmail_sync_id(tenant_id, mailbox)
    state = _read(records, sync_id)
    history_id = state.get("history_id") or ""
    page_token = state.get("page_token") or ""
    if history_id:
        if _poll_history(tenant_id, connection_id, mailbox, client, graph, records, sync_id, group, history_id):
            return
    _poll_full(tenant_id, connection_id, mailbox, client, graph, records, sync_id, group, page_token)


def _poll_history(
    tenant_id: str,
    connection_id: str,
    mailbox: str,
    client,
    graph,
    records,
    sync_id: str,
    group: str,
    history_id: str,
) -> bool:
    """Return True when polling should stop for this tick."""
    page_token = None
    latest_history = history_id
    while True:
        started = time.perf_counter()
        try:
            page = client.list_history(history_id, page_token)
        except GmailRequestError as exc:
            if exc.status == 404:
                _write(records, sync_id, _blank_state(tenant_id, connection_id, mailbox))
                return False
            raise
        history = page.get("history") if isinstance(page.get("history"), list) else []
        failed = 0
        for entry in history:
            if not isinstance(entry, dict):
                continue
            for added in entry.get("messagesAdded") or []:
                if not isinstance(added, dict):
                    continue
                raw = added.get("message")
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                full = client.get_message(raw["id"])
                outcome = _ingest(full, tenant_id, connection_id, mailbox, graph)
                if outcome == "blocked":
                    failed = 1
                    break
            if failed:
                break
            for removed in entry.get("messagesDeleted") or []:
                if not isinstance(removed, dict):
                    continue
                raw = removed.get("message")
                if not isinstance(raw, dict) or not raw.get("id"):
                    continue
                tomb = {"id": raw["id"], "internalDate": "0", "payload": {"headers": []}, "snippet": ""}
                outcome = _ingest(tomb, tenant_id, connection_id, mailbox, graph, deleted=True)
                if outcome == "blocked":
                    failed = 1
                    break
            if failed:
                break
        if failed:
            return True
        if isinstance(page.get("historyId"), str):
            latest_history = page["historyId"]
        next_token = page.get("nextPageToken")
        if not next_token:
            _write(
                records,
                sync_id,
                {
                    "tenant_id": tenant_id,
                    "connection_id": connection_id,
                    "mailbox": mailbox,
                    "history_id": latest_history,
                    "page_token": "",
                    "status": "complete",
                    "last_error_class": "",
                },
            )
            records.note_source_sync(tenant_id, gmail_source_id(tenant_id, mailbox), group)
            index_source(graph, records, gmail_source_id(tenant_id, mailbox))
            logger.info(
                "%s",
                format_document_log(
                    tenant_id=tenant_id,
                    folder_id=mailbox,
                    records=len(history),
                    failed=0,
                    duration_ms=_elapsed(started),
                    error_class="",
                ),
            )
            return True
        page_token = next_token


def _poll_full(
    tenant_id: str,
    connection_id: str,
    mailbox: str,
    client,
    graph,
    records,
    sync_id: str,
    group: str,
    page_token: str,
) -> None:
    started = time.perf_counter()
    page = client.list_messages(page_token or None)
    messages = page.get("messages") if isinstance(page.get("messages"), list) else []
    failed = 0
    latest_history = ""
    for stub in messages:
        if not isinstance(stub, dict) or not stub.get("id"):
            continue
        full = client.get_message(stub["id"])
        if isinstance(full.get("historyId"), str):
            latest_history = full["historyId"]
        outcome = _ingest(full, tenant_id, connection_id, mailbox, graph)
        if outcome == "blocked":
            failed = 1
            break
    if failed:
        return
    next_token = page.get("nextPageToken") or ""
    finished = next_token == ""
    _write(
        records,
        sync_id,
        {
            "tenant_id": tenant_id,
            "connection_id": connection_id,
            "mailbox": mailbox,
            "history_id": latest_history if finished else "",
            "page_token": "" if finished else next_token,
            "status": "complete" if finished else "in_progress",
            "last_error_class": "",
        },
    )
    if finished and latest_history:
        records.note_source_sync(tenant_id, gmail_source_id(tenant_id, mailbox), group)
        index_source(graph, records, gmail_source_id(tenant_id, mailbox))
    logger.info(
        "%s",
        format_document_log(
            tenant_id=tenant_id,
            folder_id=mailbox,
            records=len(messages),
            failed=0,
            duration_ms=_elapsed(started),
            error_class="",
        ),
    )
    if not finished:
        _poll_full(tenant_id, connection_id, mailbox, client, graph, records, sync_id, group, next_token)


def _ingest(raw, tenant_id, connection_id, mailbox, graph, deleted=False) -> str:
    message = normalize_gmail_message(
        raw,
        tenant_id=tenant_id,
        connection_id=connection_id,
        mailbox=mailbox,
        deleted=deleted,
    )
    result = upsert_gmail_message(graph, message)
    return "blocked" if not result.applied else "ok"


def _read(records, sync_id: str) -> dict:
    row = records.get(sync_id)
    payload = row["payload"] if row else {}
    return {
        "history_id": payload.get("history_id") or "",
        "page_token": payload.get("page_token") or "",
    }


def _write(records, sync_id: str, payload: dict) -> None:
    records.merge(sync_id, payload.get("tenant_id", ""), SYNC_STATE, payload)


def _blank_state(tenant_id: str, connection_id: str, mailbox: str) -> dict:
    return {
        "tenant_id": tenant_id,
        "connection_id": connection_id,
        "mailbox": mailbox,
        "history_id": "",
        "page_token": "",
        "status": "in_progress",
        "last_error_class": "",
    }


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
