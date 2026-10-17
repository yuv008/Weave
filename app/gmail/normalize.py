"""Map one Gmail message into a canonical record."""

import hashlib
import re
from datetime import UTC, datetime

from app.models.canonical_gmail import CanonicalGmailMessage
from app.models.canonical import EXCERPT_LIMIT

_EMAIL = re.compile(r"<([^>]+)>|([^\s<>]+@[^\s<>]+)")


def parse_email_address(value: str) -> str | None:
    if not isinstance(value, str) or not value.strip():
        return None
    match = _EMAIL.search(value)
    if not match:
        return None
    return (match.group(1) or match.group(2) or "").strip().lower()


def _headers(payload: dict) -> dict[str, str]:
    rows = payload.get("headers") if isinstance(payload, dict) else None
    if not isinstance(rows, list):
        return {}
    result: dict[str, str] = {}
    for row in rows:
        if isinstance(row, dict) and isinstance(row.get("name"), str) and isinstance(row.get("value"), str):
            result[row["name"].lower()] = row["value"]
    return result


def _version_from_internal_date(raw: dict) -> str:
    value = raw.get("internalDate")
    if isinstance(value, str) and value.isdigit():
        millis = int(value)
        return f"{millis:020d}"
    if isinstance(value, int):
        return f"{value:020d}"
    raise ValueError("Gmail internalDate is required")


def _occurred_at(raw: dict) -> datetime:
    millis = raw.get("internalDate")
    if isinstance(millis, str) and millis.isdigit():
        return datetime.fromtimestamp(int(millis) / 1000, tz=UTC)
    if isinstance(millis, int):
        return datetime.fromtimestamp(millis / 1000, tz=UTC)
    raise ValueError("Gmail internalDate is required")


def normalize_gmail_message(
    raw: dict,
    *,
    tenant_id: str,
    connection_id: str,
    mailbox: str,
    deleted: bool = False,
) -> CanonicalGmailMessage:
    if not isinstance(raw, dict):
        raise ValueError("Gmail payload must be an object")
    message_id = raw.get("id")
    if not isinstance(message_id, str) or not message_id:
        raise ValueError("Gmail message id is required")
    payload = raw.get("payload")
    if not isinstance(payload, dict):
        payload = {}
    headers = _headers(payload)
    subject = headers.get("subject", "")
    snippet = raw.get("snippet") if isinstance(raw.get("snippet"), str) else ""
    labels = raw.get("labelIds") if isinstance(raw.get("labelIds"), list) else []
    is_deleted = deleted or "TRASH" in labels
    excerpt_text = "" if is_deleted else f"{subject}: {snippet}".strip(": ").strip()
    author = parse_email_address(headers.get("from", ""))
    thread_id = raw.get("threadId") if isinstance(raw.get("threadId"), str) else None
    parent_external_id = None
    in_reply = headers.get("in-reply-to", "")
    if isinstance(in_reply, str) and in_reply.strip():
        token = in_reply.strip().split()[-1].strip("<>")
        if token:
            parent_external_id = token
    elif thread_id and thread_id != message_id:
        parent_external_id = thread_id
    occurred = _occurred_at(raw)
    version = _version_from_internal_date(raw)
    body_key = f"{subject}\n{snippet}"
    return CanonicalGmailMessage(
        tenant_id=tenant_id,
        connection_id=connection_id,
        mailbox=mailbox,
        external_id=message_id,
        thread_parent_external_id=parent_external_id,
        author_email=author,
        occurred_at=occurred,
        version=version,
        deleted=is_deleted,
        source_url=f"https://mail.google.com/mail/u/0/#inbox/{message_id}",
        excerpt=excerpt_text[:EXCERPT_LIMIT],
        content_hash=hashlib.sha256(body_key.encode("utf-8")).hexdigest(),
    )
