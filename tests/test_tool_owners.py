from agents.tools.owners import find_owner
from tests.gate_support import fixture_resolver
from tests.tool_support import live, make_ctx, scratch  # noqa: F401

pytestmark = live


def test_owner_and_experts_for_sam(scratch):
    fx, _, graph = scratch
    result = find_owner(make_ctx(fx, graph, "sam"), "SSO")
    assert [o["name"] for o in result.data["owners"]] == ["Dev"]
    experts = result.data["experts"]
    assert [e["name"] for e in experts] == ["Dev", "Maya"]
    assert experts[0]["score"] > experts[1]["score"]
    assert fx.ids["item_eng_switch_b"] in experts[0]["evidence"]


def test_scores_use_visible_items_only(scratch):
    fx, _, graph = scratch
    priya = find_owner(make_ctx(fx, graph, "priya"), "SSO")
    assert [e["name"] for e in priya.data["experts"]] == ["Dev"]
    cited = {c.item_id for c in priya.citations}
    assert not any("C0ENGSSO" in item for item in cited)
    assert priya.hidden == 3

    resolver, store = fixture_resolver(fx)
    full = find_owner(make_ctx(fx, graph, "sam", resolver=resolver), "SSO")
    store.members[fx.ids["sam"]].discard("slack:C0ENGSSO")
    store.epoch += 1
    revoked = find_owner(make_ctx(fx, graph, "sam", resolver=resolver), "SSO")
    assert [e["name"] for e in revoked.data["experts"]] == ["Dev"]
    assert revoked.data["experts"][0]["score"] < full.data["experts"][0]["score"]


def test_customer_owner_comes_from_deal(scratch):
    fx, _, graph = scratch
    result = find_owner(make_ctx(fx, graph, "priya"), "Acme Corp")
    assert [(o["name"], o["owns"]) for o in result.data["owners"]] == [("Priya", fx.ids["deal_acme"])]


def test_unknown_subject(scratch):
    fx, _, graph = scratch
    result = find_owner(make_ctx(fx, graph, "sam"), "nothing-like-this")
    assert result.data["owners"] == [] and result.message
