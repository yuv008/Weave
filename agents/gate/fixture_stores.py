"""In-memory gate data built from a fixture, for offline tests and demos."""

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime

from agents.gate.resolve import AclSnapshot, PersonInfo, hash_token
from fixtures.acme import Fixture
from shared.schema import Label, Prop, Rel


@dataclass
class MemoryGateStore:
    tenant: str
    members: dict[str, set[str]] = field(default_factory=dict)
    synced: dict[str, datetime] = field(default_factory=dict)
    epoch: int = 1
    tokens: dict[str, str] = field(default_factory=dict)

    def token_owner(self, token_sha256: str) -> tuple[str, str] | None:
        person = self.tokens.get(token_sha256)
        return (self.tenant, person) if person else None

    def snapshot(self, tenant: str) -> AclSnapshot:
        if tenant != self.tenant:
            return AclSnapshot({}, {}, 0)
        return AclSnapshot(
            {person: set(groups) for person, groups in self.members.items()},
            dict(self.synced),
            self.epoch,
        )

    def add_token(self, token: str, person_id: str) -> None:
        self.tokens[hash_token(token)] = person_id


@dataclass
class MemoryDirectory:
    people: dict[str, PersonInfo]

    def person(self, tenant: str, person_id: str) -> PersonInfo | None:
        return self.people.get(person_id)

    def levels(self, tenant: str, person_ids: Iterable[str]) -> dict[str, int]:
        return {p: self.people[p].level for p in person_ids if p in self.people}


def from_fixture(fx: Fixture, synced_at: datetime) -> tuple[MemoryGateStore, MemoryDirectory]:
    members: dict[str, set[str]] = defaultdict(set)
    for membership in fx.memberships:
        members[membership["person_id"]].add(membership["acl_group"])
    store = MemoryGateStore(
        tenant=fx.tenant,
        members=dict(members),
        synced={s["acl_group"]: synced_at for s in fx.source_sync},
    )

    reports_to: dict[str, str] = {e.src: e.dst for e in fx.edges if e.rel == Rel.REPORTS_TO}

    def below(manager: str) -> tuple[str, ...]:
        direct = [p for p, m in reports_to.items() if m == manager]
        return tuple(direct + [s for d in direct for s in below(d)])

    people = {
        n.id: PersonInfo(n.id, n.props[Prop.LEVEL], n.props[Prop.DEPT], below(n.id))
        for n in fx.nodes
        if n.label == Label.PERSON
    }
    return store, MemoryDirectory(people)
