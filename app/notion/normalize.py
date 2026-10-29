"""Map one Notion page into a canonical document."""

import hashlib
from datetime import UTC, datetime

from app.models.canonical import EXCERPT_LIMIT
from app.models.canonical_document import CanonicalDocument
from app.notion.time import sortable_timestamp


def normalize_notion_page(
    page: dict,
    plain_text: str,
    *,
    tenant_id: str,
    connection_id: str,
    workspace_id: str,
) -> CanonicalDocument | None:
    if not isinstance(page, dict):
        raise ValueError("Notion page must be an object")
    if page.get("object") != "page":
        return None
    page_id = page.get("id")
    if not isinstance(page_id, str) or not page_id:
        raise ValueError("Notion page id is required")
    edited = page.get("last_edited_time")
    created = page.get("created_time") or edited
    if not isinstance(edited, str) or not isinstance(created, str):
        raise ValueError("Notion timestamps are required")
    deleted = bool(page.get("in_trash") or page.get("archived"))
    text = "" if deleted else plain_text
    url = page.get("url")
    if not isinstance(url, str) or not url:
        url = f"https://www.notion.so/{page_id.replace('-', '')}"
    author_id, author_email, author_name = _created_by(page)
    return CanonicalDocument(
        tenant_id=tenant_id,
        connection_id=connection_id,
        source="notion",
        workspace_id=workspace_id,
        external_id=page_id,
        title="" if deleted else _title(page),
        occurred_at=_parse(created),
        updated_at=_parse(edited),
        version=sortable_timestamp(edited),
        deleted=deleted,
        source_url=url,
        excerpt="" if deleted else text[:EXCERPT_LIMIT],
        body=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        author_external_id=author_id,
        author_email=author_email,
        author_name=author_name,
    )


def _created_by(page: dict) -> tuple[str, str, str]:
    created = page.get("created_by")
    if not isinstance(created, dict):
        return "", "", ""
    author_id = created.get("id") if isinstance(created.get("id"), str) else ""
    person = created.get("person") if isinstance(created.get("person"), dict) else {}
    email = person.get("email") if isinstance(person.get("email"), str) else ""
    name = person.get("name") if isinstance(person.get("name"), str) else ""
    return author_id, email.strip().lower(), name.strip()


def _title(page: dict) -> str:
    properties = page.get("properties")
    if not isinstance(properties, dict):
        return ""
    for prop in properties.values():
        if isinstance(prop, dict) and prop.get("type") == "title":
            rich = prop.get("title") or []
            return "".join(
                item.get("plain_text", "")
                for item in rich
                if isinstance(item, dict) and isinstance(item.get("plain_text"), str)
            )[:200]
    return ""


def _parse(value: str) -> datetime:
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)
