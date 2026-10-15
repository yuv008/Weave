"""One timestamp format, so string comparison matches time order.

Format: UTC ISO-8601 at seconds precision, ``2026-10-12T09:30:00+00:00``.
Dates without a time (``due``, ``renewal_date``) are ``YYYY-MM-DD``.
"""

from datetime import UTC, datetime


def to_ts(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0).isoformat()


def parse_ts(value: str) -> datetime:
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must carry a UTC offset")
    return parsed


def is_ts(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return to_ts(parse_ts(value)) == value
    except ValueError:
        return False
