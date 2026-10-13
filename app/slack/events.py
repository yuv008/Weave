"""Slack Events API handling for public channels.

Slack's event payload uses ``channel`` for a public channel. ``group``,
``im``, and ``mpim`` are ignored. A missing channel type is accepted only
when backfill already created that conversation.
"""

import hashlib
import hmac
import logging
from datetime import UTC, datetime

from app.graph.upsert import upsert_message
from app.ingest.state import event_already_applied, mark_event_applied
from app.models.canonical import conversation_node_id
from app.slack.normalize import PUBLIC_CHANNEL, normalize_slack_message

logger = logging.getLogger("weave.slack.events")
_MAX_SKEW_SECONDS = 60 * 5
_PUBLIC_EVENT_TYPES = {PUBLIC_CHANNEL, "channel"}


def verify_slack_signature(
    secret: str,
    timestamp: str,
    body: bytes,
    signature: str,
    *,
    now: float,
) -> bool:
    if not secret or not timestamp or not signature:
        return False
    try:
        stamp = int(timestamp)
    except ValueError:
        return False
    if abs(now - stamp) > _MAX_SKEW_SECONDS:
        return False
    digest = hmac.new(
        secret.encode("utf-8"),
        f"v0:{timestamp}:".encode("utf-8") + body,
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(f"v0={digest}", signature)


def apply_slack_event(
    payload: dict,
    graph,
    *,
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
    workspace_domain: str | None = None,
) -> dict:
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        return {"challenge": challenge if isinstance(challenge, str) else ""}
    if payload.get("type") != "event_callback":
        return {"ok": True}
    event = payload.get("event")
    if not isinstance(event, dict) or event.get("type") != "message":
        return {"ok": True}
    channel_id = event.get("channel") if isinstance(event.get("channel"), str) else ""
    if not _is_public_event(event, graph, tenant_id, workspace_id, channel_id):
        logger.info("slack_event_skip channel_id=%s", channel_id)
        return {"ok": True}
    event_id = payload.get("event_id") if isinstance(payload.get("event_id"), str) else ""
    if event_already_applied(graph, event_id):
        return {"ok": True}
    message = normalize_slack_message(
        event,
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=workspace_id,
        channel_id=channel_id,
        channel_type=PUBLIC_CHANNEL,
        workspace_domain=workspace_domain,
    )
    if message is None:
        return {"ok": True}
    upsert_message(graph, message, now=datetime.now(UTC))
    mark_event_applied(graph, event_id, tenant_id)
    return {"ok": True}


def _is_public_event(event: dict, graph, tenant_id: str, workspace_id: str, channel_id: str) -> bool:
    channel_type = event.get("channel_type")
    if isinstance(channel_type, str) and channel_type:
        return channel_type in _PUBLIC_EVENT_TYPES
    if not channel_id:
        return False
    rows = graph.query(
        "MATCH (c:Conversation {id: $id}) RETURN c.id",
        {"id": conversation_node_id(tenant_id, workspace_id, channel_id)},
    ).result_set
    return bool(rows)
