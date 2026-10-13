"""Canonical document shared by Notion and Google Drive.

The full body is not a field. Callers hash it, copy a 280-character prefix
into ``excerpt``, and drop the rest.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.canonical import EXCERPT_LIMIT


def document_node_id(tenant_id: str, source: str, workspace_id: str, external_id: str) -> str:
    return f"{tenant_id}:{source}:document:{workspace_id}:{external_id}"


class CanonicalDocument(BaseModel):
    """One Notion page or Google Doc, already normalized."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    connection_id: str
    source: str
    workspace_id: str
    external_id: str
    title: str = ""
    occurred_at: datetime
    updated_at: datetime
    version: str
    deleted: bool = False
    source_url: str = ""
    excerpt: str = Field(default="", max_length=EXCERPT_LIMIT)
    content_hash: str
    visibility: str = "demo_single_tenant"

    @field_validator("tenant_id", "connection_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value or ":" in value or "/" in value:
            raise ValueError("identifier must be a non-empty string without ':' or '/'")
        return value

    @field_validator("source")
    @classmethod
    def _source(cls, value: str) -> str:
        if value not in {"notion", "gdrive"}:
            raise ValueError("document source must be notion or gdrive")
        return value

    @field_validator("visibility")
    @classmethod
    def _demo_scope(cls, value: str) -> str:
        if value != "demo_single_tenant":
            raise ValueError("visibility must stay demo_single_tenant")
        return value

    @field_validator("content_hash")
    @classmethod
    def _sha256(cls, value: str) -> str:
        if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
            raise ValueError("content_hash must be a sha256 hex digest")
        return value

    @field_validator("workspace_id", "external_id", "version")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("value is required")
        return value

    @property
    def node_id(self) -> str:
        return document_node_id(self.tenant_id, self.source, self.workspace_id, self.external_id)
