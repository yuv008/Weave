"""Shared expectations for the gate tests: the visibility matrix and a Python cell evaluator."""

from datetime import UTC, datetime

from agents.gate.fixture_stores import from_fixture
from agents.gate.principal import Principal, Surface
from agents.gate.resolve import Resolver
from agents.gate.rules import item_is_visible, projection_is_visible
from fixtures.acme import Fixture
from shared.schema import Label, Rel

NOW = datetime(2026, 10, 15, 12, 0, tzinfo=UTC)
PEOPLE = ("priya", "rohan", "dev", "maya", "sam")

RAW, SUMMARY, NONE = "raw", "summary", "none"

# The visibility matrix on page 15 of the architecture doc. Rows are sources,
# columns follow PEOPLE: Priya L1 sales, Rohan L3 sales head, Dev L1 eng,
# Maya L2 eng lead, Sam L0 eng intern.
MATRIX = {
    "src_eng_public": (RAW, RAW, RAW, RAW, RAW),
    "src_eng_repo": (NONE, NONE, RAW, RAW, RAW),
    "src_eng_leads": (NONE, NONE, NONE, RAW, NONE),
    "src_leadership": (SUMMARY, RAW, SUMMARY, RAW, SUMMARY),
    "src_budget": (NONE, RAW, NONE, NONE, NONE),
}

# The demo-story sources outside the matrix. #eng-sso reaches Sales as a
# summary through the approved projection to #deal-acme.
STORY = {
    "src_eng_sso": (SUMMARY, SUMMARY, RAW, RAW, RAW),
    "src_deal": (RAW, RAW, NONE, NONE, NONE),
    "src_notion": (RAW, RAW, NONE, NONE, NONE),
    "src_roadmap": (RAW, RAW, RAW, RAW, RAW),
}


def cells(table: dict[str, tuple[str, ...]]) -> list[tuple[str, str, str]]:
    return [
        (person, source, row[index])
        for source, row in table.items()
        for index, person in enumerate(PEOPLE)
    ]


def fixture_resolver(fx: Fixture, synced_at: datetime = NOW, now: datetime = NOW, stale_minutes: int = 15):
    store, directory = from_fixture(fx, synced_at)
    return Resolver(store, directory, stale_minutes=stale_minutes, clock=lambda: now), store


def person_principal(fx: Fixture, who: str, resolver: Resolver | None = None) -> Principal:
    resolver = resolver or fixture_resolver(fx)[0]
    return resolver.for_person(fx.tenant, fx.ids[who], Surface.MCP)


def _visible(principal: Principal, props: dict) -> bool:
    return item_is_visible(props, principal.level, principal.groups, principal.fresh)


def python_cell(fx: Fixture, principal: Principal, source_id: str) -> str:
    in_source = {e.src for e in fx.edges if e.rel == Rel.IN_SOURCE and e.dst == source_id}
    items = [n for n in fx.nodes if n.id in in_source]
    if any(_visible(principal, i.props) for i in items):
        return RAW
    evidence_in_source = {e.src for e in fx.edges if e.rel == Rel.EVIDENCED_BY and e.dst in in_source}
    for projection in (n for n in fx.nodes if n.label == Label.PROJECTION):
        origins = {e.dst for e in fx.edges if e.rel == Rel.SUMMARIZES and e.src == projection.id}
        if origins & evidence_in_source and projection_is_visible(
            projection.props, principal.level, principal.groups
        ):
            return SUMMARY
    return NONE


def python_claim_visible(fx: Fixture, principal: Principal, claim_id: str) -> bool:
    evidence = [e.dst for e in fx.edges if e.rel == Rel.EVIDENCED_BY and e.src == claim_id]
    return any(_visible(principal, fx.node(i).props) for i in evidence)
