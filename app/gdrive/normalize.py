"""Map one Google Drive file into a canonical document."""

import hashlib

from app.gdrive.client import DOC_MIME
from app.models.canonical import EXCERPT_LIMIT
from app.models.canonical_document import CanonicalDocument
from app.notion.normalize import _parse
from app.notion.time import sortable_timestamp


def normalize_drive_file(
    metadata: dict,
    plain_text: str,
    *,
    tenant_id: str,
    connection_id: str,
    folder_id: str,
) -> CanonicalDocument | None:
    if not isinstance(metadata, dict):
        raise ValueError("Drive file must be an object")
    if metadata.get("mimeType") != DOC_MIME:
        return None
    file_id = metadata.get("id")
    modified = metadata.get("modifiedTime")
    if not isinstance(file_id, str) or not file_id or not isinstance(modified, str):
        raise ValueError("Drive file id and modifiedTime are required")
    deleted = bool(metadata.get("trashed"))
    text = "" if deleted else plain_text
    title = metadata.get("name") if isinstance(metadata.get("name"), str) else ""
    author_email, author_name = _owner(metadata)
    return CanonicalDocument(
        tenant_id=tenant_id,
        connection_id=connection_id,
        source="gdrive",
        workspace_id=folder_id,
        external_id=file_id,
        title="" if deleted else title[:200],
        occurred_at=_parse(modified),
        updated_at=_parse(modified),
        version=sortable_timestamp(modified),
        deleted=deleted,
        source_url=f"https://drive.google.com/file/d/{file_id}/view",
        excerpt="" if deleted else text[:EXCERPT_LIMIT],
        body=text,
        content_hash=hashlib.sha256(text.encode("utf-8")).hexdigest(),
        author_email=author_email,
        author_name=author_name,
    )


def _owner(metadata: dict) -> tuple[str, str]:
    owners = metadata.get("owners")
    if not isinstance(owners, list) or not owners:
        return "", ""
    first = owners[0]
    if not isinstance(first, dict):
        return "", ""
    email = first.get("emailAddress") if isinstance(first.get("emailAddress"), str) else ""
    name = first.get("displayName") if isinstance(first.get("displayName"), str) else ""
    return email.strip().lower(), name.strip()
