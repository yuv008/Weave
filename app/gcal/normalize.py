"""Map one Google Calendar event into a canonical document."""

import hashlib

from app.gmail.normalize import parse_email_address
from app.models.canonical import EXCERPT_LIMIT
from app.models.canonical_document import CanonicalDocument
from app.notion.normalize import _parse
from app.notion.time import sortable_timestamp


def normalize_calendar_event(
    raw: dict,
    *,
    tenant_id: str,
    connection_id: str,
    calendar_id: str,
) -> CanonicalDocument | None:
    if not isinstance(raw, dict):
        raise ValueError("Calendar event must be an object")
    event_id = raw.get("id")
    updated = raw.get("updated")
    if not isinstance(event_id, str) or not event_id or not isinstance(updated, str):
        raise ValueError("Calendar event id and updated are required")
    deleted = raw.get("status") == "cancelled"
    summary = raw.get("summary") if isinstance(raw.get("summary"), str) else ""
    start = _event_time(raw.get("start"))
    end = _event_time(raw.get("end"))
    organizer = raw.get("organizer") if isinstance(raw.get("organizer"), dict) else {}
    author = parse_email_address(organizer.get("email", "")) or ""
    excerpt_text = "" if deleted else f"{summary} | {start} - {end}".strip()
    return CanonicalDocument(
        tenant_id=tenant_id,
        connection_id=connection_id,
        source="gcal",
        workspace_id=calendar_id,
        external_id=event_id,
        title="" if deleted else summary[:200],
        occurred_at=_parse(start or updated),
        updated_at=_parse(updated),
        version=sortable_timestamp(updated),
        deleted=deleted,
        source_url=raw.get("htmlLink") if isinstance(raw.get("htmlLink"), str) else "",
        excerpt=excerpt_text[:EXCERPT_LIMIT],
        content_hash=hashlib.sha256(excerpt_text.encode("utf-8")).hexdigest(),
        kind="event",
        author_external_id=author,
    )


def _event_time(value) -> str:
    if not isinstance(value, dict):
        return ""
    if isinstance(value.get("dateTime"), str):
        return value["dateTime"]
    if isinstance(value.get("date"), str):
        return value["date"]
    return ""
