import pytest

from agents.tools.base import RestrictedCitation, ToolInputError
from agents.tools.decisions import get_decision, trace_decision
from shared.config import load_config
from shared.schema import Rel
from tests.gate_support import fixture_resolver
from tests.tool_support import live, make_ctx, scratch  # noqa: F401

pytestmark = live


def test_sam_gets_active_decision_with_history(scratch):
    fx, _, graph = scratch
    result = get_decision(make_ctx(fx, graph, "sam"), topic="sso")
    [card] = result.data["decisions"]
    assert card["id"] == fx.ids["decision_provider_b"]
    assert card["statement"] == "Use Provider B for SSO."
    assert set(card["evidence"]) == {fx.ids["item_eng_switch_b"], fx.ids["item_roadmap_issue"]}
    assert card["replaces"]["statement"] == "Use Provider A for SSO."
    assert card["likely"] is False and result.data["conflict"] is False
    assert {c.author for c in result.citations} == {"Dev"}
    assert all(c.url for c in result.citations)
    assert (fx.ids["decision_provider_b"], Rel.SUPERSEDES, fx.ids["decision_provider_a"]) in {
        (e.src, e.rel, e.dst) for e in result.path
    }


def test_priya_sees_only_roadmap_evidence_and_no_replaced_decision(scratch):
    fx, _, graph = scratch
    cfg = load_config()
    result = get_decision(make_ctx(fx, graph, "priya", cfg=cfg), topic="SSO")
    [card] = result.data["decisions"]
    assert card["evidence"] == [fx.ids["item_roadmap_issue"]]
    assert card["supersedes"] == [] and card["replaces"] is None
    assert result.hidden >= 2
    public = result.public(cfg)
    assert "hidden_count" not in public
    assert fx.ids["decision_provider_a"] not in str(public)


def test_projection_when_raw_evidence_is_hidden(scratch):
    fx, _, graph = scratch
    resolver, store = fixture_resolver(fx)
    del store.synced["github:demo-org/roadmap"]
    result = get_decision(make_ctx(fx, graph, "priya", resolver=resolver), topic="SSO")
    [card] = result.data["decisions"]
    assert card["restricted"] is True and "statement" not in card
    assert card["summary"].startswith("Engineering switched the SSO provider")
    [cite] = result.citations
    assert isinstance(cite, RestrictedCitation) and cite.source == "restricted"
    assert not hasattr(cite, "url")


def test_repo_picks_the_subject(scratch):
    fx, _, graph = scratch
    sam = get_decision(make_ctx(fx, graph, "sam"), repo="demo-org/platform")
    assert sam.data["subject"]["id"] == fx.ids["project_sso"]
    assert sam.data["decisions"][0]["restricted"] is True
    maya = get_decision(make_ctx(fx, graph, "maya"), repo="demo-org/platform")
    assert maya.data["decisions"][0]["statement"] == "SSO ships before any new feature work in Q4."


def test_as_of_returns_what_was_in_force(scratch):
    fx, _, graph = scratch
    sam = get_decision(make_ctx(fx, graph, "sam"), topic="SSO", as_of="2026-10-05")
    assert [c["id"] for c in sam.data["decisions"]] == [fx.ids["decision_provider_a"]]
    priya = get_decision(make_ctx(fx, graph, "priya"), topic="SSO", as_of="2026-10-05")
    assert priya.data["decisions"] == [] and "search_context" in priya.message
    with pytest.raises(ToolInputError):
        get_decision(make_ctx(fx, graph, "sam"), topic="SSO", as_of="last week")


def test_decision_id_shows_newer_decision(scratch):
    fx, _, graph = scratch
    result = get_decision(make_ctx(fx, graph, "sam"), decision_id=fx.ids["decision_provider_a"])
    [card] = result.data["decisions"]
    assert [d["id"] for d in card["superseded_by"]] == [fx.ids["decision_provider_b"]]


def test_conflicting_active_decisions_shown_side_by_side(scratch):
    fx, raw, graph = scratch
    conflict = "acme:decision:conflict-test"
    raw.query(
        "MATCH (t:Topic {id: $topic}), (i:Item {id: $item}) "
        "CREATE (d:Decision {id: $id, tenant_id: 'acme', statement: 'Keep Provider A for SSO.', status: 'active', "
        "valid_from: '2026-10-13T09:00:00+00:00', confidence: 0.6, last_verified_at: '2026-10-13T09:00:00+00:00', "
        "extractor_version: 'test'})-[:ABOUT]->(t), (d)-[:EVIDENCED_BY {quote: 'x', span: [0, 1], confidence: 0.6}]->(i)",
        {"topic": fx.ids["topic_sso"], "item": fx.ids["item_eng_public_update"], "id": conflict},
    )
    try:
        result = get_decision(make_ctx(fx, graph, "sam"), topic="SSO")
        assert result.data["conflict"] is True
        assert {c["id"] for c in result.data["decisions"]} == {conflict, fx.ids["decision_provider_b"]}
        assert next(c for c in result.data["decisions"] if c["id"] == conflict)["likely"] is True
    finally:
        raw.query("MATCH (d:Decision {id: $id}) DETACH DELETE d", {"id": conflict})


def test_unknown_topic_and_missing_input(scratch):
    fx, _, graph = scratch
    result = get_decision(make_ctx(fx, graph, "sam"), topic="kubernetes")
    assert result.data["decisions"] == [] and "search_context" in result.message
    with pytest.raises(ToolInputError):
        get_decision(make_ctx(fx, graph, "sam"))


def test_trace_names_visible_hops_and_counts_hidden(scratch):
    fx, _, graph = scratch
    sam = trace_decision(make_ctx(fx, graph, "sam"), fx.ids["decision_provider_a"])
    evidence = {e["item_id"]: e for e in sam.data["evidence"]}
    assert set(evidence) == {fx.ids["item_eng_choose_a"], fx.ids["item_eng_agree_a"]}
    assert {e["author"] for e in evidence.values()} == {"Dev", "Maya"}
    assert evidence[fx.ids["item_eng_choose_a"]]["thread"][0]["item_id"] == fx.ids["item_eng_agree_a"]
    assert sam.hidden == 0

    cfg = load_config()
    priya = trace_decision(make_ctx(fx, graph, "priya", cfg=cfg), fx.ids["decision_provider_b"])
    assert [e["item_id"] for e in priya.data["evidence"]] == [fx.ids["item_roadmap_issue"]]
    assert priya.hidden >= 1
    assert "hidden_count" not in priya.public(cfg)
    noisy = cfg.model_copy(update={"hidden_sources_notice": True})
    assert priya.public(noisy)["hidden_count"] == priya.hidden


def test_trace_of_invisible_decision_is_not_found(scratch):
    fx, _, graph = scratch
    hidden = trace_decision(make_ctx(fx, graph, "priya"), fx.ids["decision_provider_a"])
    missing = trace_decision(make_ctx(fx, graph, "priya"), "acme:decision:does-not-exist")
    assert hidden.data == missing.data and hidden.message == missing.message
