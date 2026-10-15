"""search_context against the seeded Neon chunk table and co:acme. Needs WEAVE_LIVE_TESTS=1."""

from datetime import timedelta

import pytest

from agents.tools.base import ToolInputError
from agents.tools.search import search_context
from tests.gate_support import NOW, fixture_resolver
from tests.tool_support import live, make_ctx

pytestmark = live


@pytest.fixture(scope="module")
def acme():
    from shared.env import load_env

    load_env()
    from agents.graph import open_company
    from fixtures.acme import build
    from shared.pg import connect_pg

    return build("acme"), open_company("acme"), connect_pg


def _hits(result):
    return [h["item_id"] for h in result.data["hits"]]


def test_hits_only_from_visible_chunks(acme):
    fx, graph, pg = acme
    sam = search_context(make_ctx(fx, graph, "sam", pg_connect=pg), "budget")
    assert sam.data["hits"] == [] and sam.message
    rohan = search_context(make_ctx(fx, graph, "rohan", pg_connect=pg), "budget")
    assert fx.ids["item_budget_sheet"] in _hits(rohan)
    assert rohan.data["mode"] == "keyword"
    maya = search_context(make_ctx(fx, graph, "maya", pg_connect=pg), "budget")
    assert fx.ids["item_leadership_budget"] in _hits(maya)
    assert fx.ids["item_budget_sheet"] not in _hits(maya)


def test_hits_expand_one_hop(acme):
    fx, graph, pg = acme
    result = search_context(make_ctx(fx, graph, "priya", pg_connect=pg), "provider switched")
    hit = next(h for h in result.data["hits"] if h["item_id"] == fx.ids["item_roadmap_issue"])
    assert [c["id"] for c in hit["claims"]] == [fx.ids["decision_provider_b"]]
    assert fx.ids["topic_sso"] in [m["id"] for m in hit["mentions"]]
    assert not any("C0ENGSSO" in item for item in _hits(result))


def test_stale_org_wide_source_drops_out(acme):
    fx, graph, pg = acme
    fresh = search_context(make_ctx(fx, graph, "priya", pg_connect=pg), "SSO roadmap")
    assert fx.ids["item_eng_public_update"] in _hits(fresh)
    resolver, store = fixture_resolver(fx)
    store.synced["slack:C0ENGPUB"] = NOW - timedelta(hours=1)
    stale = search_context(make_ctx(fx, graph, "priya", resolver=resolver, pg_connect=pg), "SSO roadmap")
    assert fx.ids["item_eng_public_update"] not in _hits(stale)
    assert fx.ids["item_roadmap_issue"] in _hits(stale)


def test_system_filter_and_input_checks(acme):
    fx, graph, pg = acme
    ctx = make_ctx(fx, graph, "sam", pg_connect=pg)
    result = search_context(ctx, "provider", system="github")
    assert result.data["hits"] and all(":github:" in item for item in _hits(result))
    with pytest.raises(ToolInputError):
        search_context(ctx, "provider", system="email")
    with pytest.raises(ToolInputError):
        search_context(ctx, "   ")
