"""Canonical document shared by Notion and Google Drive.

Callers hash the full body and copy a 280-character prefix into ``excerpt``
for the graph. ``body`` carries the full text to the chunk table only; it is
never written to the graph.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.canonical import EXCERPT_LIMIT
from shared.ids import document_id, github_issue_id


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
    body: str = Field(default="", repr=False, exclude=True)
    content_hash: str
    visibility: str = "demo_single_tenant"
    kind: str = ""
    status: str = ""
    author_external_id: str = ""
    author_email: str = ""
    author_name: str = ""
    parent_external_id: str | None = None
    repo_slug: str = ""

    @field_validator("tenant_id", "connection_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value or ":" in value or "/" in value:
            raise ValueError("identifier must be a non-empty string without ':' or '/'")
        return value

    @field_validator("source")
    @classmethod
    def _source(cls, value: str) -> str:
        if value not in {"notion", "gdrive", "github", "gcal", "hubspot"}:
            raise ValueError("document source must be notion, gdrive, github, gcal, or hubspot")
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

    @field_validator("kind")
    @classmethod
    def _kind(cls, value: str) -> str:
        if value not in {"", "issue", "pull_request", "comment", "event", "company", "deal"}:
            raise ValueError("kind is invalid")
        return value

    @field_validator("status")
    @classmethod
    def _status(cls, value: str) -> str:
        if value not in {"", "open", "closed", "merged"}:
            raise ValueError("status is invalid")
        return value

    @field_validator("parent_external_id")
    @classmethod
    def _parent(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return value

    @field_validator("workspace_id", "external_id", "version")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("value is required")
        return value

    @property
    def node_id(self) -> str:
        if self.source == "github" and self.repo_slug and self.kind in {"issue", "pull_request"}:
            return github_issue_id(self.tenant_id, self.repo_slug, _issue_number(self.external_id))
        if self.source == "github" and self.repo_slug and self.kind == "comment":
            return document_id(self.tenant_id, "github", self.repo_slug, self.external_id)
        return document_node_id(self.tenant_id, self.source, self.workspace_id, self.external_id)

    @property
    def parent_node_id(self) -> str | None:
        if not self.parent_external_id:
            return None
        if self.source == "github" and self.repo_slug and self.parent_external_id.startswith("issue:"):
            return github_issue_id(self.tenant_id, self.repo_slug, _issue_number(self.parent_external_id))
        return document_node_id(
            self.tenant_id, self.source, self.workspace_id, self.parent_external_id
        )


def _issue_number(external_id: str) -> int:
    return int(external_id.split(":", 1)[1])
