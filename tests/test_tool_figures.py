from datetime import timedelta

from agents.tools.figures import get_figures
from tests.gate_support import NOW
from tests.tool_support import live, make_ctx, scratch  # noqa: F401

pytestmark = live


def test_rohan_sees_budget_with_history(scratch):
    fx, _, graph = scratch
    result = get_figures(make_ctx(fx, graph, "rohan"), "SSO integration")
    [figure] = result.data["figures"]
    assert figure["id"] == fx.ids["figure_q4_budget"]
    assert (figure["value"], figure["unit"], figure["stale"]) == (250000, "USD", False)
    assert figure["sources"] == ["#leadership", "Leadership budget sheet"]
    assert [h["value"] for h in figure["history"]] == [200000]


def test_maya_sees_channel_evidence_only(scratch):
    fx, _, graph = scratch
    result = get_figures(make_ctx(fx, graph, "maya"), "SSO integration")
    [figure] = result.data["figures"]
    assert figure["sources"] == ["#leadership"]
    assert figure["evidence"] == [fx.ids["item_leadership_budget"]]


def test_sam_gets_nothing_without_hint(scratch):
    fx, _, graph = scratch
    sam = get_figures(make_ctx(fx, graph, "sam"), "SSO integration")
    unknown = get_figures(make_ctx(fx, graph, "sam"), "Kubernetes")
    assert sam.data["figures"] == [] and sam.citations == []
    assert sam.message == unknown.message


def test_stale_badge(scratch):
    fx, _, graph = scratch
    later = make_ctx(fx, graph, "priya", clock=lambda: NOW + timedelta(days=40))
    [figure] = get_figures(later, "Acme renewal").data["figures"]
    assert figure["stale"] is True
    [fresh] = get_figures(make_ctx(fx, graph, "priya"), "Acme renewal", name="arr").data["figures"]
    assert fresh["stale"] is False and fresh["value"] == 120000
    assert get_figures(make_ctx(fx, graph, "priya"), "Acme renewal", name="budget").data["figures"] == []
