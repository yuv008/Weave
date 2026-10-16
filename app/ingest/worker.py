"""Backfill public Slack channels and retry failed messages.

Checkpoints move only after the page's graph writes, including thread
replies, have returned. ``oldest`` advances only when the history cursor
is exhausted.
"""

import logging
import os
import time

from app.graph.policy import allowed_source, slack_source_id
from app.graph.upsert import ensure_indexes, upsert_message
from app.ingest.chunks import index_source
from app.ingest.log import log_page
from app.ingest.records import open_store
from app.ingest.state import (
    channel_sync_id,
    delete_failure,
    failure_attempts,
    failure_id,
    read_channel_sync,
    read_thread_sync,
    record_failure,
    retryable_failures,
    thread_sync_id,
    write_channel_sync,
    write_connection,
    write_thread_sync,
)
from app.models.canonical import message_node_id
from app.slack.client import SlackClient, SlackRequestError
from app.slack.normalize import PUBLIC_CHANNEL, format_slack_version, normalize_slack_message

_MAX_FAILURES = 5
logger = logging.getLogger("weave.ingest")


def slack_client_from_env() -> SlackClient:
    """Process edge for the bot token. The token is not logged."""

    token = os.environ.get("SLACK_BOT_TOKEN", "")
    return SlackClient(token)


def backfill_public_channels(
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
    client,
    graph,
    *,
    workspace_domain: str | None = None,
    incremental: bool = False,
    store=None,
) -> None:
    records = open_store(store)
    ensure_indexes(graph)
    write_connection(
        graph,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=workspace_id,
    )
    cursor = None
    while True:
        page = client.list_public_channels(cursor)
        for channel in page.get("channels") or []:
            if not _is_public_channel(channel):
                continue
            _sync_channel(
                tenant_id=tenant_id,
                connection_id=connection_id,
                workspace_id=workspace_id,
                channel_id=channel["id"],
                client=client,
                graph=graph,
                workspace_domain=workspace_domain,
                incremental=incremental,
                store=records,
            )
        cursor = _next_cursor(page)
        if not cursor:
            break
    from app.slack.people import enrich_slack_authors

    enrich_slack_authors(graph, client, tenant_id)


def retry_failures(graph, client, *, workspace_domain: str | None = None, store=None) -> None:
    """Refetch failed messages one at a time. This does not call history."""

    records = open_store(store)
    for row in retryable_failures(records):
        (
            node_id,
            tenant_id,
            connection_id,
            workspace_id,
            channel_id,
            message_ts,
            stored_domain,
        ) = row
        domain = workspace_domain if workspace_domain is not None else stored_domain or None
        started = time.perf_counter()
        try:
            payload = client.fetch_message(channel_id, message_ts)
            raw = _message_with_ts(payload, message_ts)
            message = normalize_slack_message(
                raw,
                tenant_id=tenant_id,
                connection_id=connection_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                channel_type=PUBLIC_CHANNEL,
                workspace_domain=domain or None,
            )
            if message is None:
                raise ValueError("non-public message")
            upsert_message(graph, message)
        except ValueError:
            record_failure(
                records,
                node_id=node_id,
                fields=_failure_fields(
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    workspace_id=workspace_id,
                    channel_id=channel_id,
                    message_ts=message_ts,
                    workspace_domain=domain or "",
                    error_class="ValueError",
                ),
            )
            log_page(
                logger,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                records=0,
                failed=1,
                duration_ms=_elapsed(started),
                error_class="ValueError",
            )
            continue
        delete_failure(records, node_id)
        log_page(
            logger,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            records=1,
            failed=0,
            duration_ms=_elapsed(started),
            error_class="",
        )


def _sync_channel(
    *,
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
    channel_id: str,
    client,
    graph,
    workspace_domain: str | None,
    incremental: bool,
    store,
) -> None:
    found = allowed_source("slack", channel_id)
    if found is None:
        return
    group, _policy = found
    sync_id = channel_sync_id(tenant_id, workspace_id, channel_id)
    state = read_channel_sync(store, sync_id)
    if state["status"] == "complete" and not incremental:
        return
    cursor = "" if incremental and state["status"] == "complete" else state["history_cursor"]
    oldest = state["oldest"] or "0"
    newest = state["newest_ts"]
    if not _join_public_channel(client, channel_id):
        log_page(
            logger,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            records=0,
            failed=1,
            duration_ms=0,
            error_class="not_in_channel",
        )
        return
    while True:
        started = time.perf_counter()
        try:
            page = client.history(channel_id, cursor or None, oldest)
        except SlackRequestError as exc:
            log_page(
                logger,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                records=0,
                failed=1,
                duration_ms=_elapsed(started),
                error_class=exc.error,
            )
            return
        messages = page.get("messages") or []
        try:
            blocked = _ingest_page(
                messages,
                tenant_id=tenant_id,
                connection_id=connection_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                client=client,
                graph=graph,
                workspace_domain=workspace_domain,
                follow_replies=True,
                store=store,
            )
        except Exception as exc:
            log_page(
                logger,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                records=len(messages),
                failed=1,
                duration_ms=_elapsed(started),
                error_class=type(exc).__name__,
            )
            raise
        if blocked:
            log_page(
                logger,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                records=len(messages),
                failed=1,
                duration_ms=_elapsed(started),
                error_class="ValueError",
            )
            return
        for raw in messages:
            ts = raw.get("ts")
            if isinstance(ts, str) and ts:
                newest = _later(newest, ts)
        next_cursor = _next_cursor(page)
        finished = next_cursor == ""
        write_channel_sync(
            store,
            sync_id=sync_id,
            fields={
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "workspace_id": workspace_id,
                "channel_id": channel_id,
                "history_cursor": "" if finished else next_cursor,
                "oldest": newest if finished and newest else oldest,
                "newest_ts": newest,
                "status": "complete" if finished else "in_progress",
                "last_error_class": "",
            },
        )
        source = slack_source_id(tenant_id, workspace_id, channel_id)
        store.note_source_sync(tenant_id, source, group)
        index_source(graph, store, source)
        log_page(
            logger,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            records=len(messages),
            failed=0,
            duration_ms=_elapsed(started),
            error_class="",
        )
        if finished:
            return
        cursor = next_cursor


def _sync_replies(
    *,
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
    channel_id: str,
    parent_ts: str,
    client,
    graph,
    workspace_domain: str | None,
    store,
) -> bool:
    sync_id = thread_sync_id(tenant_id, workspace_id, channel_id, parent_ts)
    cursor = read_thread_sync(store, sync_id)["replies_cursor"]
    while True:
        started = time.perf_counter()
        page = client.replies(channel_id, parent_ts, cursor or None)
        messages = page.get("messages") or []
        blocked = _ingest_page(
            messages,
            tenant_id=tenant_id,
            connection_id=connection_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            client=client,
            graph=graph,
            workspace_domain=workspace_domain,
            follow_replies=False,
            store=store,
        )
        if blocked:
            log_page(
                logger,
                tenant_id=tenant_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                records=len(messages),
                failed=1,
                duration_ms=_elapsed(started),
                error_class="ValueError",
            )
            return True
        next_cursor = _next_cursor(page)
        write_thread_sync(
            store,
            sync_id=sync_id,
            fields={
                "tenant_id": tenant_id,
                "connection_id": connection_id,
                "workspace_id": workspace_id,
                "channel_id": channel_id,
                "parent_ts": parent_ts,
                "replies_cursor": next_cursor,
                "status": "complete" if next_cursor == "" else "in_progress",
            },
        )
        log_page(
            logger,
            tenant_id=tenant_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            records=len(messages),
            failed=0,
            duration_ms=_elapsed(started),
            error_class="",
        )
        if next_cursor == "":
            return False
        cursor = next_cursor


def _ingest_page(
    messages: list,
    *,
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
    channel_id: str,
    client,
    graph,
    workspace_domain: str | None,
    follow_replies: bool,
    store,
) -> bool:
    accepted: list[dict] = []
    for raw in messages:
        if not isinstance(raw, dict):
            _remember_malformed(
                store,
                tenant_id=tenant_id,
                connection_id=connection_id,
                workspace_id=workspace_id,
                channel_id=channel_id,
                raw={},
                workspace_domain=workspace_domain,
            )
            return True
        outcome = _ingest_one(
            raw,
            tenant_id=tenant_id,
            connection_id=connection_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            graph=graph,
            workspace_domain=workspace_domain,
            store=store,
        )
        if outcome == "blocked":
            return True
        if outcome == "ok":
            accepted.append(raw)
    if follow_replies:
        for raw in accepted:
            if _reply_count(raw) > 0 and _is_thread_parent(raw):
                blocked = _sync_replies(
                    tenant_id=tenant_id,
                    connection_id=connection_id,
                    workspace_id=workspace_id,
                    channel_id=channel_id,
                    parent_ts=raw["ts"],
                    client=client,
                    graph=graph,
                    workspace_domain=workspace_domain,
                    store=store,
                )
                if blocked:
                    return True
    return False


def _ingest_one(
    raw: dict,
    *,
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
    channel_id: str,
    graph,
    workspace_domain: str | None,
    store,
) -> str:
    ts = _message_ts(raw)
    node = failure_id(tenant_id, channel_id, ts or "missing")
    if failure_attempts(store, node) >= _MAX_FAILURES:
        return "skipped"
    try:
        message = normalize_slack_message(
            raw,
            tenant_id=tenant_id,
            connection_id=connection_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            channel_type=PUBLIC_CHANNEL,
            workspace_domain=workspace_domain,
        )
    except ValueError:
        _remember_malformed(
            store,
            tenant_id=tenant_id,
            connection_id=connection_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            raw=raw,
            workspace_domain=workspace_domain,
        )
        return "blocked"
    if message is None:
        raise RuntimeError("public backfill produced an empty message")
    upsert_message(graph, message)
    delete_failure(store, node)
    return "ok"


def _remember_malformed(store, *, tenant_id, connection_id, workspace_id, channel_id, raw, workspace_domain):
    ts = _message_ts(raw) or "missing"
    record_failure(
        store,
        node_id=failure_id(tenant_id, channel_id, ts),
        fields=_failure_fields(
            tenant_id=tenant_id,
            connection_id=connection_id,
            workspace_id=workspace_id,
            channel_id=channel_id,
            message_ts=ts,
            workspace_domain=workspace_domain or "",
            error_class="ValueError",
        ),
    )


def _failure_fields(
    *,
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
    channel_id: str,
    message_ts: str,
    workspace_domain: str,
    error_class: str,
) -> dict[str, str]:
    version = ""
    node_id = ""
    if message_ts and message_ts != "missing":
        try:
            version = format_slack_version(message_ts)
        except ValueError:
            version = ""
        else:
            node_id = message_node_id(tenant_id, f"{channel_id}:{message_ts}")
    return {
        "node_id": node_id,
        "version": version,
        "error_class": error_class,
        "tenant_id": tenant_id,
        "connection_id": connection_id,
        "workspace_id": workspace_id,
        "channel_id": channel_id,
        "message_ts": message_ts,
        "workspace_domain": workspace_domain,
    }


def _message_with_ts(payload: dict, ts: str) -> dict:
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        raise ValueError("missing message")
    for message in messages:
        if isinstance(message, dict) and message.get("ts") == ts:
            return message
    raise ValueError("missing message")


def _message_ts(raw: dict) -> str | None:
    if raw.get("subtype") == "message_deleted" and isinstance(raw.get("deleted_ts"), str):
        return raw["deleted_ts"]
    ts = raw.get("ts")
    if isinstance(ts, str) and ts:
        return ts
    return None


def _join_public_channel(client, channel_id: str) -> bool:
    join = getattr(client, "join_channel", None)
    if join is None:
        return True
    try:
        join(channel_id)
    except SlackRequestError as exc:
        if exc.error == "already_in_channel":
            return True
        return False
    return True


def _is_public_channel(channel: dict) -> bool:
    if not isinstance(channel, dict):
        return False
    if channel.get("is_private") or channel.get("is_im") or channel.get("is_mpim"):
        return False
    if channel.get("is_channel") is False:
        return False
    return isinstance(channel.get("id"), str) and bool(channel.get("id"))


def _is_thread_parent(raw: dict) -> bool:
    thread_ts = raw.get("thread_ts")
    ts = raw.get("ts")
    return not isinstance(thread_ts, str) or thread_ts == "" or thread_ts == ts


def _reply_count(raw: dict) -> int:
    value = raw.get("reply_count") or 0
    if isinstance(value, bool) or not isinstance(value, int):
        return 0
    return value


def _next_cursor(page: dict) -> str:
    metadata = page.get("response_metadata") if isinstance(page, dict) else None
    if not isinstance(metadata, dict):
        return ""
    cursor = metadata.get("next_cursor") or ""
    return cursor if isinstance(cursor, str) else ""


def _later(current: str, candidate: str) -> str:
    if not current:
        return candidate
    if format_slack_version(candidate) > format_slack_version(current):
        return candidate
    return current


def _elapsed(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
