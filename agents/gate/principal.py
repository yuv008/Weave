"""Who is asking. Built by the resolver from a token and a surface, never from tool arguments."""

import hashlib
from dataclasses import dataclass
from enum import StrEnum


class Surface(StrEnum):
    WEB = "web"
    MCP = "mcp"
    SLACK_DM = "slack_dm"
    SLACK_CHANNEL = "slack_channel"
    SLACK_GROUP_DM = "slack_group_dm"


PRIVATE_SURFACES = frozenset({Surface.WEB, Surface.MCP, Surface.SLACK_DM})
SHARED_SURFACES = frozenset({Surface.SLACK_CHANNEL, Surface.SLACK_GROUP_DM})


class Kind(StrEnum):
    PERSON = "person"
    CHANNEL = "channel"


@dataclass(frozen=True)
class Principal:
    tenant: str
    kind: Kind
    surface: Surface
    level: int
    groups: frozenset[str]
    acl_epoch: int
    fresh: frozenset[str] = frozenset()
    user_id: str | None = None
    dept: str | None = None
    channel_group: str | None = None

    @property
    def reads_private(self) -> bool:
        """Personal graph and semantic memory are read only for a person on a private surface."""

        return self.kind is Kind.PERSON and self.surface in PRIVATE_SURFACES

    @property
    def visibility_hash(self) -> str:
        key = "|".join(
            (self.tenant, self.kind, str(self.level), str(self.acl_epoch),
             *sorted(self.groups), "fresh", *sorted(self.fresh))
        )
        return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
