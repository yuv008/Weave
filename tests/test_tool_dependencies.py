import pytest

from agents.tools.base import ToolInputError
from agents.tools.dependencies import affected_owners, dept_rollup, get_dependencies, impact_of
from shared.schema import Rel
from tests.tool_support import live, make_ctx, scratch  # noqa: F401

pytestmark = live


def _edges(result):
    return {(e["src"], e["rel"], e["dst"]): e for e in result.data["edges"]}


def test_dependencies_labelled_by_department(scratch):
    fx, _, graph = scratch
    result = get_dependencies(make_ctx(fx, graph, "priya"), "Acme renewal", depth=2)
    edges = _edges(result)
    needs = edges[(fx.ids["deal_acme"], Rel.NEEDS, fx.ids["project_sso"])]
    assert (needs["src_dept"], needs["dst_dept"], needs["cross_department"]) == ("Sales", "Engineering", True)
    assert needs["evidence"] == [fx.ids["item_notion_deal"]]
    assert (fx.ids["commitment_acme_sso"], Rel.DEPENDS_ON, fx.ids["project_sso"]) in edges
    assert fx.ids["item_notion_deal"] in {c.item_id for c in result.citations}


def test_edges_with_hidden_evidence_are_omitted(scratch):
    fx, _, graph = scratch
    maya = get_dependencies(make_ctx(fx, graph, "maya"), "SSO integration", direction="upstream")
    assert maya.data["edges"] == [] and maya.hidden == 2
    priya = get_dependencies(make_ctx(fx, graph, "priya"), fx.ids["commitment_acme_sso"], direction="downstream")
    # C1 also depends on D1, which Priya cannot see; that edge is counted, not shown.
    assert set(_edges(priya)) == {(fx.ids["commitment_acme_sso"], Rel.DEPENDS_ON, fx.ids["project_sso"])}
    assert priya.hidden == 1


def test_dependency_input_checks(scratch):
    fx, _, graph = scratch
    with pytest.raises(ToolInputError):
        get_dependencies(make_ctx(fx, graph, "priya"), "Acme renewal", depth=4)
    with pytest.raises(ToolInputError):
        get_dependencies(make_ctx(fx, graph, "priya"), "Acme renewal", direction="sideways")
    hidden_claim = get_dependencies(make_ctx(fx, graph, "maya"), fx.ids["commitment_acme_sso"])
    assert hidden_claim.data["root"] is None and hidden_claim.message


def test_impact_for_priya_keeps_root_restricted(scratch):
    fx, _, graph = scratch
    change = f"{fx.ids['decision_provider_a']}:superseded"
    result = impact_of(make_ctx(fx, graph, "priya"), change)
    root = result.data["root"]
    assert root["restricted"] is True and "name" not in root and root.get("dept") is None
    assert [r["id"] for r in root["replaced_by"]] == [fx.ids["decision_provider_b"]]
    assert [a["id"] for a in result.data["affected"]] == [fx.ids["commitment_acme_sso"]]
    assert [n["name"] for n in result.data["notify"]] == ["Priya"]
    [edge] = result.data["edges"]
    assert edge["dst_dept"] is None


def test_impact_for_engineering_shows_root_but_no_sales_work(scratch):
    fx, _, graph = scratch
    result = impact_of(make_ctx(fx, graph, "maya"), fx.ids["decision_provider_a"])
    assert result.data["root"]["name"] == "Use Provider A for SSO."
    assert result.data["affected"] == [] and result.hidden == 1
    unknown = impact_of(make_ctx(fx, graph, "maya"), "acme:decision:nope")
    assert unknown.message and unknown.data["root"] is None


def test_affected_owners_picks_recipients(scratch):
    fx, _, graph = scratch
    assert affected_owners(graph, fx.ids["decision_provider_a"]) == [fx.ids["priya"]]


@pytest.mark.parametrize("who,rows", [
    ("priya", [("Sales", "Engineering", 1, 0, 0)]),
    ("rohan", [("Sales", "Engineering", 1, 0, 0)]),
    ("maya", []),
    ("dev", []),
    ("sam", []),
])
def test_rollup_counts_visible_edges_only(scratch, who, rows):
    fx, _, graph = scratch
    result = dept_rollup(make_ctx(fx, graph, who))
    actual = [(r["from_dept"], r["to_dept"], r["requests"], r["blocks"], r["at_risk"]) for r in result.data["rows"]]
    assert actual == rows


def test_rollup_department_filter(scratch):
    fx, _, graph = scratch
    ctx = make_ctx(fx, graph, "priya")
    assert dept_rollup(ctx, from_dept="engineering").data["rows"] == []
    assert len(dept_rollup(ctx, from_dept="sales", to_dept="Engineering").data["rows"]) == 1
