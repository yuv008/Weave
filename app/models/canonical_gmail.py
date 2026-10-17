"""Canonical Gmail message."""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.canonical import EXCERPT_LIMIT
from shared.ids import gmail_message_id, person_id


class CanonicalGmailMessage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    connection_id: str
    mailbox: str
    external_id: str
    thread_parent_external_id: str | None = None
    author_email: str | None = None
    occurred_at: datetime
    version: str
    deleted: bool = False
    source_url: str = ""
    excerpt: str = Field(default="", max_length=EXCERPT_LIMIT)
    content_hash: str
    visibility: str = "demo_single_tenant"

    @field_validator("tenant_id", "connection_id", "mailbox", "external_id", "version")
    @classmethod
    def _required(cls, value: str) -> str:
        if not value:
            raise ValueError("value is required")
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

    @property
    def node_id(self) -> str:
        return gmail_message_id(self.tenant_id, self.mailbox, self.external_id)

    @property
    def person_node_id(self) -> str | None:
        if not self.author_email:
            return None
        return person_id(self.tenant_id, self.author_email)

    @property
    def parent_node_id(self) -> str | None:
        if not self.thread_parent_external_id:
            return None
        return gmail_message_id(self.tenant_id, self.mailbox, self.thread_parent_external_id)
