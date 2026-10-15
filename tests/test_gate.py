from datetime import timedelta

import pytest

from agents.gate.principal import Kind, Surface
from agents.gate.resolve import NotAuthorized
from agents.gate.rules import drop_stale, item_is_visible, person_groups
from agents.gate.visible import (
    chunk_visible_sql,
    item_visible,
    keep_visible_claims,
    keep_visible_edges,
    projection_visible,
)
from agents.graph import ReadOnlyGraph, open_private
from fixtures.acme import build
from shared.acl import ORG_WIDE
from shared.schema import CLAIM_LABELS, Label, ProjectionStatus
from tests.gate_support import (
    MATRIX,
    NOW,
    NONE,
    PEOPLE,
    RAW,
    STORY,
    SUMMARY,
    cells,
    fixture_resolver,
    person_principal,
    python_cell,
    python_claim_visible,
)


@pytest.fixture(scope="module")
def fx():
    return build("acme")


@pytest.mark.parametrize("who,source,expected", cells(MATRIX))
def test_visibility_matrix(fx, who, source, expected):
    principal = person_principal(fx, who)
    assert python_cell(fx, principal, fx.ids[source]) == expected


@pytest.mark.parametrize("who,source,expected", cells(STORY))
def test_story_sources(fx, who, source, expected):
    principal = person_principal(fx, who)
    assert python_cell(fx, principal, fx.ids[source]) == expected


def test_oversight_inherits_reports_groups(fx):
    rohan = person_principal(fx, "rohan")
    priya = person_principal(fx, "priya")
    maya = person_principal(fx, "maya")
    assert priya.groups <= rohan.groups
    assert person_principal(fx, "sam").groups <= maya.groups
    assert "slack:C0ENGSSO" not in rohan.groups
    assert person_principal(fx, "dev").groups == {"slack:C0ENGSSO", "github:demo-org/platform", ORG_WIDE}


def test_oversight_needs_level_two():
    memberships = {"boss": {"slack:A"}, "report": {"slack:B"}}
    assert person_groups("boss", 1, memberships, ["report"]) == {"slack:A", ORG_WIDE}
    assert person_groups("boss", 2, memberships, ["report"]) == {"slack:A", "slack:B", ORG_WIDE}


def test_channel_audience_hides_management_facts(fx):
    resolver, _ = fixture_resolver(fx)
    channel = resolver.for_channel(
        fx.tenant, "slack:C0ENGSSO", [fx.ids["dev"], fx.ids["maya"], fx.ids["sam"]], Surface.SLACK_CHANNEL
    )
    assert channel.kind is Kind.CHANNEL
    assert channel.level == 0
    assert channel.groups == {"slack:C0ENGSSO", ORG_WIDE}
    assert not channel.reads_private
    assert not python_claim_visible(fx, channel, fx.ids["figure_q4_budget"])
    assert python_claim_visible(fx, person_principal(fx, "maya"), fx.ids["figure_q4_budget"])


def test_channel_with_unknown_member_takes_level_zero(fx):
    resolver, _ = fixture_resolver(fx)
    channel = resolver.for_channel(
        fx.tenant, "slack:C0LEADERS", [fx.ids["rohan"], fx.ids["maya"], "acme:person:guest@x.example"],
        Surface.SLACK_CHANNEL,
    )
    assert channel.level == 0
    assert not python_claim_visible(fx, channel, fx.ids["figure_q4_budget"])


def test_personal_graph_never_opens_on_shared_surface(fx):
    resolver, _ = fixture_resolver(fx)
    channel = resolver.for_channel(fx.tenant, "slack:C0ENGSSO", [fx.ids["maya"]], Surface.SLACK_CHANNEL)
    with pytest.raises(PermissionError):
        open_private(channel, db=object())
    with pytest.raises(ValueError):
        resolver.for_person(fx.tenant, fx.ids["maya"], Surface.SLACK_CHANNEL)
    with pytest.raises(ValueError):
        resolver.for_channel(fx.tenant, "slack:C0ENGSSO", [fx.ids["maya"]], Surface.MCP)


def test_draft_projection_hidden_from_everyone(fx):
    from agents.gate.rules import projection_is_visible

    draft = fx.node(fx.ids["projection_budget_draft"])
    assert draft.props["status"] == ProjectionStatus.DRAFT
    for who in PEOPLE:
        principal = person_principal(fx, who)
        assert not projection_is_visible(draft.props, principal.level, principal.groups)


def test_revocation_takes_effect_and_changes_cache_key(fx):
    resolver, store = fixture_resolver(fx)
    before = person_principal(fx, "sam", resolver)
    assert python_cell(fx, before, fx.ids["src_eng_sso"]) == RAW
    store.members[fx.ids["sam"]].discard("slack:C0ENGSSO")
    store.epoch += 1
    after = person_principal(fx, "sam", resolver)
    assert python_cell(fx, after, fx.ids["src_eng_sso"]) == NONE
    assert after.visibility_hash != before.visibility_hash
    store.members[fx.ids["sam"]].add("slack:C0ENGSSO")


def test_stale_source_hidden_for_everyone(fx):
    fresh, _ = fixture_resolver(fx, synced_at=NOW - timedelta(minutes=10))
    stale, _ = fixture_resolver(fx, synced_at=NOW - timedelta(minutes=16))
    assert python_cell(fx, person_principal(fx, "sam", fresh), fx.ids["src_eng_sso"]) == RAW
    for who in PEOPLE:
        principal = person_principal(fx, who, stale)
        assert principal.groups == {ORG_WIDE}
        assert principal.fresh == frozenset()
        for source in ("src_eng_public", "src_roadmap", "src_eng_sso"):
            assert python_cell(fx, principal, fx.ids[source]) == NONE


def test_stale_org_wide_source_hidden_while_others_stay(fx):
    resolver, store = fixture_resolver(fx)
    store.synced["slack:C0ENGPUB"] = NOW - timedelta(minutes=30)
    for who in PEOPLE:
        principal = person_principal(fx, who, resolver)
        assert ORG_WIDE in principal.groups
        assert python_cell(fx, principal, fx.ids["src_eng_public"]) == NONE
        assert python_cell(fx, principal, fx.ids["src_roadmap"]) == RAW


def test_org_wide_source_without_sync_row_fails_closed(fx):
    resolver, store = fixture_resolver(fx)
    del store.synced["github:demo-org/roadmap"]
    priya = person_principal(fx, "priya", resolver)
    # Raw access is gone; the approved summary to #deal-acme still reaches her.
    assert python_cell(fx, priya, fx.ids["src_roadmap"]) == SUMMARY
    assert not python_claim_visible(fx, priya, fx.ids["decision_provider_b"])


def test_unknown_source_fails_closed():
    assert drop_stale({"slack:NEVER", ORG_WIDE}, {}, NOW, 15) == {ORG_WIDE}


def test_privacy_flip_same_person_dm_vs_channel(fx):
    resolver, store = fixture_resolver(fx)
    store.add_token("wv_maya", fx.ids["maya"])
    budget = fx.ids["figure_q4_budget"]

    dm = resolver.for_token("wv_maya", Surface.SLACK_DM)
    assert dm.reads_private
    assert python_claim_visible(fx, dm, budget)

    channel = resolver.for_token(
        "wv_maya", Surface.SLACK_CHANNEL,
        channel_group="slack:C0ENGSSO", channel_members=[fx.ids["maya"], fx.ids["sam"]],
    )
    assert not channel.reads_private
    assert not python_claim_visible(fx, channel, budget)
    assert dm.visibility_hash != channel.visibility_hash


def test_tokens(fx):
    resolver, store = fixture_resolver(fx)
    store.add_token("wv_sam", fx.ids["sam"])
    principal = resolver.for_token("wv_sam", Surface.MCP)
    assert principal.user_id == fx.ids["sam"]
    assert principal.level == 0
    with pytest.raises(NotAuthorized) as unknown:
        resolver.for_token("wv_nobody", Surface.MCP)
    assert str(unknown.value) == "not authorized"
    with pytest.raises(NotAuthorized):
        resolver.for_token("wv_sam", Surface.SLACK_CHANNEL)
    channel = resolver.for_token(
        "wv_sam", Surface.SLACK_CHANNEL, channel_group="slack:C0ENGSSO", channel_members=[fx.ids["sam"]]
    )
    assert channel.kind is Kind.CHANNEL and channel.user_id is None


def test_claim_visibility_per_person(fx):
    claims = [n.id for n in fx.nodes if n.label in CLAIM_LABELS]
    visible = {who: {c for c in claims if python_claim_visible(fx, person_principal(fx, who), c)} for who in PEOPLE}
    assert fx.ids["figure_q4_budget"] in visible["maya"]
    assert fx.ids["figure_q4_budget"] in visible["rohan"]
    assert fx.ids["figure_q4_budget"] not in visible["sam"]
    # Maya is in the budget's channel but below the sheet's level 3; the channel is enough.
    assert fx.ids["figure_q4_budget"] in visible["maya"]
    # D2 reaches Sales through the org-wide roadmap issue; D1 lived only in #eng-sso.
    assert fx.ids["decision_provider_b"] in visible["priya"]
    assert fx.ids["decision_provider_b"] in visible["rohan"]
    assert fx.ids["decision_provider_a"] not in visible["priya"]
    assert fx.ids["decision_provider_a"] not in visible["rohan"]
    assert fx.ids["commitment_acme_sso"] not in visible["dev"]


def test_budget_sheet_needs_level_three(fx):
    sheet = fx.node_by_key("item_budget_sheet").props
    maya = person_principal(fx, "maya")
    rohan = person_principal(fx, "rohan")
    assert "gdrive:F0BUDGET" in maya.groups
    assert not item_is_visible(sheet, maya.level, maya.groups, maya.fresh)
    assert item_is_visible(sheet, rohan.level, rohan.groups, rohan.fresh)


def test_read_only_graph_has_no_write_path():
    graph = ReadOnlyGraph(object(), "co:acme")
    assert not hasattr(graph, "query")
    with pytest.raises(AttributeError):
        graph.extra = 1


def test_filters_reject_bad_aliases():
    for builder in (item_visible, keep_visible_claims, projection_visible):
        with pytest.raises(ValueError):
            builder("i) DETACH DELETE (x")
    with pytest.raises(ValueError):
        keep_visible_claims("d", carry=("t) DETACH DELETE (x",))


def test_edge_and_chunk_filters():
    step = keep_visible_edges("r", carry=("a", "b"))
    assert step.startswith("OPTIONAL MATCH (gate_ei:Item) WHERE gate_ei.id IN coalesce(r.evidence_ids, [])")
    assert step.endswith("WITH a, b, r, collect(gate_ei) AS gate_edge_evidence WHERE size(gate_edge_evidence) > 0")
    sql = chunk_visible_sql("c")
    assert "c.min_level <= %(gate_level)s" in sql
    assert "c.acl_groups && %(gate_groups)s::text[]" in sql
    assert "c.acl_groups <@ (%(gate_fresh)s::text[] || 'org:all'::text)" in sql
    with pytest.raises(ValueError):
        keep_visible_edges("r) DELETE (x")
    with pytest.raises(ValueError):
        chunk_visible_sql("chunk; DROP TABLE chunk")


def test_filters_use_gate_params_only():
    text = item_visible("i") + keep_visible_claims("d") + projection_visible("p")
    assert "$gate_level" in text and "$gate_groups" in text and "$gate_fresh" in text
    step = keep_visible_claims("d", carry=("t",), evidence_as="ev")
    assert step.startswith("WITH t, d, [(d)-[:EVIDENCED_BY]->(gate_i:Item)")
    assert step.endswith("AS ev WHERE size(ev) > 0")
