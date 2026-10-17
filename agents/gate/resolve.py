"""Turn a token and a surface into a principal.

Private surface (web, MCP, Slack DM): the asker's own principal.
Shared surface (Slack channel, group DM): a channel principal built from the
channel's members; the asker's token only authenticates the request.

Errors never say why access failed beyond "not authorized", so they cannot
hint at restricted sources.
"""

import hashlib
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from agents.gate.principal import PRIVATE_SURFACES, SHARED_SURFACES, Kind, Principal, Surface
from agents.gate.rules import channel_groups, channel_level, fresh_groups, person_groups
from shared.acl import ORG_WIDE

UNKNOWN_MEMBER_LEVEL = 0


class NotAuthorized(Exception):
    def __init__(self) -> None:
        super().__init__("not authorized")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PersonInfo:
    person_id: str
    level: int
    dept: str | None
    subordinates: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class AclSnapshot:
    memberships: dict[str, set[str]]
    last_sync: dict[str, datetime]
    acl_epoch: int


class GateStore(Protocol):
    def token_owner(self, token_sha256: str) -> tuple[str, str] | None: ...
    def snapshot(self, tenant: str) -> AclSnapshot: ...


class Directory(Protocol):
    def person(self, tenant: str, person_id: str) -> PersonInfo | None: ...
    def levels(self, tenant: str, person_ids: Iterable[str]) -> dict[str, int]: ...


class Resolver:
    def __init__(
        self,
        store: GateStore,
        directory: Directory,
        *,
        stale_minutes: int,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    ):
        self._store = store
        self._directory = directory
        self._stale_minutes = stale_minutes
        self._clock = clock

    def for_token(
        self,
        token: str,
        surface: Surface,
        *,
        channel_group: str | None = None,
        channel_members: Iterable[str] = (),
    ) -> Principal:
        owner = self._store.token_owner(hash_token(token))
        if owner is None:
            raise NotAuthorized()
        tenant, person_id = owner
        if surface in SHARED_SURFACES:
            if channel_group is None:
                raise NotAuthorized()
            return self.for_channel(tenant, channel_group, channel_members, surface)
        return self.for_person(tenant, person_id, surface)

    def for_person(self, tenant: str, person_id: str, surface: Surface) -> Principal:
        if surface not in PRIVATE_SURFACES:
            raise ValueError("a person principal is only for private surfaces")
        from concurrent.futures import ThreadPoolExecutor

        with ThreadPoolExecutor(max_workers=1) as pool:
            pending = pool.submit(self._store.snapshot, tenant)
            info = self._directory.person(tenant, person_id)
            acl = pending.result()
        if info is None:
            raise NotAuthorized()
        groups = person_groups(person_id, info.level, acl.memberships, info.subordinates)
        fresh = self._fresh(acl)
        return Principal(
            tenant=tenant,
            kind=Kind.PERSON,
            surface=surface,
            level=info.level,
            groups=frozenset(g for g in groups if g == ORG_WIDE or g in fresh),
            acl_epoch=acl.acl_epoch,
            fresh=frozenset(fresh),
            user_id=person_id,
            dept=info.dept,
        )

    def for_channel(
        self, tenant: str, channel_group: str, member_ids: Iterable[str], surface: Surface
    ) -> Principal:
        if surface not in SHARED_SURFACES:
            raise ValueError("a channel principal is only for shared surfaces")
        members = list(member_ids)
        known = self._directory.levels(tenant, members)
        level = channel_level(known.get(m, UNKNOWN_MEMBER_LEVEL) for m in members)
        acl = self._store.snapshot(tenant)
        fresh = self._fresh(acl)
        return Principal(
            tenant=tenant,
            kind=Kind.CHANNEL,
            surface=surface,
            level=level,
            groups=frozenset(g for g in channel_groups(channel_group) if g == ORG_WIDE or g in fresh),
            acl_epoch=acl.acl_epoch,
            fresh=frozenset(fresh),
            channel_group=channel_group,
        )

    def _fresh(self, acl: AclSnapshot) -> set[str]:
        return fresh_groups(acl.last_sync, self._clock(), self._stale_minutes)
