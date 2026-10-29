"""Source-explicit Slack message contract.

Callers hash the full message and copy a short prefix into ``excerpt`` for
the graph. ``body`` carries the full text to the chunk table only. ``visibility`` stays on the demo scope
until a real ACL model exists.
"""

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

EXCERPT_LIMIT = 280


def message_node_id(tenant_id: str, external_id: str) -> str:
    return f"{tenant_id}:slack:message:{external_id}"


def conversation_node_id(tenant_id: str, workspace_id: str, channel_id: str) -> str:
    return f"{tenant_id}:slack:conversation:{workspace_id}:{channel_id}"


def person_node_id(tenant_id: str, workspace_id: str, user_id: str) -> str:
    return f"{tenant_id}:slack:person:{workspace_id}:{user_id}"


class CanonicalMessage(BaseModel):
    """One public Slack message, already normalized."""

    model_config = ConfigDict(extra="forbid")

    tenant_id: str
    connection_id: str
    source: str = "slack"
    workspace_id: str
    object_type: str = "message"
    external_id: str
    conversation_external_id: str
    thread_parent_external_id: str | None = None
    author_external_id: str | None = None
    occurred_at: datetime
    edited_at: datetime | None = None
    version: str
    deleted: bool = False
    source_url: str = ""
    excerpt: str = Field(default="", max_length=EXCERPT_LIMIT)
    body: str = Field(default="", repr=False, exclude=True)
    content_hash: str
    visibility: str = "demo_single_tenant"
    metadata: dict[str, str | int | bool] = Field(default_factory=dict)

    @field_validator("tenant_id", "connection_id", "workspace_id")
    @classmethod
    def _identifier(cls, value: str) -> str:
        if not value or ":" in value or "/" in value:
            raise ValueError("identifier must be a non-empty string without ':' or '/'")
        return value

    @field_validator("source")
    @classmethod
    def _slack_only(cls, value: str) -> str:
        if value != "slack":
            raise ValueError("this slice accepts slack only")
        return value

    @field_validator("object_type")
    @classmethod
    def _message_only(cls, value: str) -> str:
        if value != "message":
            raise ValueError("this slice accepts messages only")
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
        return message_node_id(self.tenant_id, self.external_id)

    @property
    def conversation_node_id(self) -> str:
        return conversation_node_id(
            self.tenant_id, self.workspace_id, self.conversation_external_id
        )

    @property
    def person_node_id(self) -> str | None:
        if self.author_external_id is None:
            return None
        return person_node_id(self.tenant_id, self.workspace_id, self.author_external_id)
