"""Ack a Slack event before the answer agent runs.

Ingest and the reply are scheduled by ``spawn`` (a FastAPI background task).
A repeated ``event_id`` does not schedule a second reply. ``url_verification``
returns the challenge and schedules nothing.
"""

from collections.abc import Callable


def is_answer_event(event: dict) -> bool:
    """An @mention, a 1:1 DM, or a group DM. Edits and bot messages are not."""

    if not isinstance(event, dict) or event.get("subtype") or event.get("bot_id"):
        return False
    kind = event.get("type")
    channel_type = event.get("channel_type")
    if kind == "app_mention":
        return isinstance(event.get("channel"), str) and isinstance(event.get("user"), str)
    return (
        kind == "message"
        and channel_type in ("im", "mpim")
        and isinstance(event.get("channel"), str)
        and isinstance(event.get("user"), str)
    )


def accept(payload: dict, *, ingest: Callable[[], None], reply: Callable[[], None], spawn: Callable,
           claims, tenant: str) -> dict:
    if payload.get("type") == "url_verification":
        challenge = payload.get("challenge")
        return {"challenge": challenge if isinstance(challenge, str) else ""}
    if payload.get("type") != "event_callback":
        return {"ok": True}
    spawn(ingest)
    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    event_id = payload.get("event_id") if isinstance(payload.get("event_id"), str) else ""
    if claims is not None and is_answer_event(event) and event_id:
        channel = event.get("channel")
        thread = event.get("thread_ts") or event.get("ts") or ""
        if claims.claim(event_id, tenant, channel, str(thread)):
            spawn(reply)
    return {"ok": True}
