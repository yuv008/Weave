"""Sortable UTC timestamps for document versions."""

from datetime import UTC, datetime


def parse_utc(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def sortable_timestamp(value: str) -> str:
    return parse_utc(value).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
