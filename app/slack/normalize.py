"""Map one Slack message payload into a canonical record.

``channel_type`` uses Slack's conversation types. Only ``public_channel``
is accepted. Private channels, group DMs, and DMs are skipped because this
demo has no per-user ACL. Malformed public payloads raise ``ValueError``
so a later worker can record the failure.
"""

import hashlib
import re
from datetime import UTC, datetime

from app.models.canonical import EXCERPT_LIMIT, CanonicalMessage

_TS = re.compile(r"^\d+(\.\d+)?$")
_METADATA_KEYS = ("subtype", "bot_id", "client_msg_id")
PUBLIC_CHANNEL = "public_channel"


def format_slack_version(ts: str) -> str:
    """Encode a Slack ts so lexicographic order matches chronological order.

    Slack timestamps look like ``1403051575.000407``. Raw string comparison
    breaks when the second or fractional part changes width (``9.0`` versus
    ``10.0``, or ``123.6`` versus ``123.60``). Seconds are padded to 16
    digits and the fraction to 6.
    """

    if not isinstance(ts, str) or _TS.fullmatch(ts) is None:
        raise ValueError(f"invalid Slack ts: {ts!r}")
    seconds_text, _, fraction = ts.partition(".")
    if len(fraction) > 6:
        raise ValueError(f"Slack ts fraction is longer than 6 digits: {ts!r}")
    return f"{int(seconds_text):016d}.{fraction.ljust(6, '0')}"


def slack_ts_to_datetime(ts: str) -> datetime:
    version = format_slack_version(ts)
    seconds_text, fraction = version.split(".")
    micros = int(fraction)
    return datetime.fromtimestamp(int(seconds_text), tz=UTC).replace(microsecond=micros)


def normalize_slack_message(
    raw: dict,
    *,
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
    channel_id: str,
    channel_type: str,
    workspace_domain: str | None = None,
) -> CanonicalMessage | None:
    """Return a canonical message, or None when the channel is not public."""

    if channel_type != PUBLIC_CHANNEL:
        return None
    if not isinstance(raw, dict):
        raise ValueError("Slack payload must be an object")

    if raw.get("subtype") == "message_changed":
        inner = raw.get("message")
        if not isinstance(inner, dict):
            raise ValueError("message_changed is missing its message object")
        raw = inner

    deleted = raw.get("subtype") == "message_deleted"
    if deleted:
        message_ts = _required_ts(raw, "deleted_ts")
        version_ts = _required_ts(raw, "ts")
        previous = raw.get("previous_message")
        author = previous.get("user") if isinstance(previous, dict) else None
        text = ""
        edited_ts = None
    else:
        message_ts = _required_ts(raw, "ts")
        edited = raw.get("edited")
        edited_ts = edited.get("ts") if isinstance(edited, dict) else None
        if edited_ts is not None:
            edited_ts = _as_ts(edited_ts, "edited.ts")
        version_ts = edited_ts or message_ts
        author = raw.get("user")
        text = raw.get("text") or ""
        if not isinstance(text, str):
            raise ValueError("message text must be a string")

    if not isinstance(channel_id, str) or not channel_id:
        raise ValueError("channel_id is required")

    thread_ts = raw.get("thread_ts")
    if deleted and not isinstance(thread_ts, str):
        previous = raw.get("previous_message")
        if isinstance(previous, dict):
            thread_ts = previous.get("thread_ts")
    parent_external_id = None
    if isinstance(thread_ts, str) and thread_ts and thread_ts != message_ts:
        parent_external_id = f"{channel_id}:{_as_ts(thread_ts, 'thread_ts')}"

    author_id = author if isinstance(author, str) and author else None
    full_text = "" if deleted else text
    return CanonicalMessage(
        tenant_id=tenant_id,
        connection_id=connection_id,
        workspace_id=workspace_id,
        external_id=f"{channel_id}:{message_ts}",
        conversation_external_id=channel_id,
        thread_parent_external_id=parent_external_id,
        author_external_id=author_id,
        occurred_at=slack_ts_to_datetime(message_ts),
        edited_at=slack_ts_to_datetime(edited_ts) if edited_ts else None,
        version=format_slack_version(version_ts),
        deleted=deleted,
        source_url=_archive_url(workspace_domain, channel_id, message_ts),
        excerpt="" if deleted else full_text[:EXCERPT_LIMIT],
        body=full_text,
        content_hash=hashlib.sha256(full_text.encode("utf-8")).hexdigest(),
        metadata=_metadata(raw),
    )


def _required_ts(payload: dict, key: str) -> str:
    if key not in payload:
        raise ValueError(f"missing {key}")
    return _as_ts(payload[key], key)


def _as_ts(value: object, label: str) -> str:
    if not isinstance(value, str):
        raise ValueError(f"{label} must be a string")
    format_slack_version(value)
    return value


def _archive_url(domain: str | None, channel_id: str, ts: str) -> str:
    if domain is None or domain == "":
        return ""
    if not isinstance(domain, str):
        raise ValueError("workspace_domain must be a string")
    host = domain.removesuffix(".slack.com").strip()
    if not host or any(char in host for char in "/:?#@ "):
        raise ValueError("invalid workspace domain")
    compact = ts.replace(".", "")
    return f"https://{host}.slack.com/archives/{channel_id}/p{compact}"


def _metadata(raw: dict) -> dict[str, str | int | bool]:
    metadata: dict[str, str | int | bool] = {}
    for key in _METADATA_KEYS:
        value = raw.get(key)
        if isinstance(value, str) and 0 < len(value) <= 128:
            metadata[key] = value
        elif type(value) is int or type(value) is bool:
            metadata[key] = value
    return metadata
