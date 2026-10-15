"""Gate against the live stores: co:acme on FalkorDB and the contract tables on Postgres.

Runs only with WEAVE_LIVE_TESTS=1. Reads .env.local / .env for connections.
Seed first: python -m scripts.seed_fixture --skip-embeddings
"""

import os
import secrets
from datetime import timedelta

import pytest

from agents.gate.principal import Surface
from agents.gate.resolve import Resolver, hash_token
from agents.gate.visible import gate_params, item_visible, keep_visible_claims, projection_visible
from fixtures.acme import build
from shared.schema import CLAIM_LABELS, Label, Prop, Rel
from tests.gate_support import (
    MATRIX,
    NONE,
    PEOPLE,
    RAW,
    STORY,
    SUMMARY,
    person_principal,
    python_claim_visible,
)

pytestmark = pytest.mark.skipif(os.environ.get("WEAVE_LIVE_TESTS") != "1", reason="set WEAVE_LIVE_TESTS=1")

RAW_Q = f"""
MATCH (i:{Label.ITEM})-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE} {{{Prop.ID}: $source}})
WHERE {item_visible("i")}
RETURN count(i)
"""

SUMMARY_Q = f"""
MATCH (p:{Label.PROJECTION})-[:{Rel.SUMMARIZES}]->(c)-[:{Rel.EVIDENCED_BY}]->(i:{Label.ITEM})
      -[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE} {{{Prop.ID}: $source}})
WHERE {projection_visible("p")}
RETURN count(DISTINCT p)
"""

CLAIMS_Q = f"""
MATCH (d)
WHERE {" OR ".join(f"d:{label}" for label in CLAIM_LABELS)}
{keep_visible_claims("d")}
RETURN collect(d.{Prop.ID})
"""


@pytest.fixture(scope="module")
def live():
    from shared.env import load_env

    load_env()
    from agents.gate.stores import GraphDirectory, PostgresGateStore
    from agents.graph import open_company
    from shared.pg import connect_pg

    fx = build("acme")
    graph = open_company(fx.tenant)
    store = PostgresGateStore(connect_pg)
    synced = store.snapshot(fx.tenant).last_sync
    assert synced, "seed Postgres first"
    clock_now = max(synced.values()) + timedelta(minutes=1)
    resolver = Resolver(store, GraphDirectory(graph), stale_minutes=15, clock=lambda: clock_now)
    return fx, graph, store, resolver


def _cell(graph, principal, source_id: str) -> str:
    params = {**gate_params(principal), "source": source_id}
    if graph.ro_query(RAW_Q, params).result_set[0][0] > 0:
        return RAW
    if graph.ro_query(SUMMARY_Q, params).result_set[0][0] > 0:
        return SUMMARY
    return NONE


def test_live_principals_match_offline(live):
    fx, _, _, resolver = live
    for who in PEOPLE:
        principal = resolver.for_person(fx.tenant, fx.ids[who], Surface.MCP)
        offline = person_principal(fx, who)
        assert principal.groups == offline.groups, who
        assert principal.level == offline.level


@pytest.mark.parametrize("table", [MATRIX, STORY], ids=["matrix", "story"])
def test_live_visibility_matrix(live, table):
    fx, graph, _, resolver = live
    principals = {who: resolver.for_person(fx.tenant, fx.ids[who], Surface.MCP) for who in PEOPLE}
    actual = {
        source: tuple(_cell(graph, principals[who], fx.ids[source]) for who in PEOPLE)
        for source in table
    }
    assert actual == table


class _AgedSource:
    """Wraps the live store and moves one source's last sync an hour back."""

    def __init__(self, store, group: str):
        self._store, self._group = store, group

    def token_owner(self, token_sha256):
        return self._store.token_owner(token_sha256)

    def snapshot(self, tenant):
        acl = self._store.snapshot(tenant)
        acl.last_sync[self._group] -= timedelta(hours=1)
        return acl


def test_live_stale_org_wide_source_hidden(live):
    fx, graph, store, resolver = live
    aged = Resolver(_AgedSource(store, "slack:C0ENGPUB"), resolver._directory,
                    stale_minutes=15, clock=resolver._clock)
    sam = aged.for_person(fx.tenant, fx.ids["sam"], Surface.MCP)
    assert "slack:C0ENGPUB" not in sam.fresh
    assert _cell(graph, sam, fx.ids["src_eng_public"]) == NONE
    assert _cell(graph, sam, fx.ids["src_roadmap"]) == RAW


def test_live_claim_filter_matches_python(live):
    fx, graph, _, resolver = live
    claims = [n.id for n in fx.nodes if n.label in CLAIM_LABELS]
    for who in PEOPLE:
        principal = resolver.for_person(fx.tenant, fx.ids[who], Surface.MCP)
        cypher = set(graph.ro_query(CLAIMS_Q, gate_params(principal)).result_set[0][0])
        python = {c for c in claims if python_claim_visible(fx, principal, c)}
        assert cypher == python, who


def test_live_channel_principal_hides_budget(live):
    fx, graph, _, resolver = live
    channel = resolver.for_channel(
        fx.tenant, "slack:C0ENGSSO", [fx.ids["dev"], fx.ids["maya"], fx.ids["sam"]], Surface.SLACK_CHANNEL
    )
    visible = set(graph.ro_query(CLAIMS_Q, gate_params(channel)).result_set[0][0])
    assert fx.ids["figure_q4_budget"] not in visible
    assert fx.ids["decision_provider_b"] in visible


def test_live_token_round_trip(live):
    fx, _, _, resolver = live
    from shared.pg import connect_pg

    token = f"wv_test_{secrets.token_urlsafe(12)}"
    conn = connect_pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_token (token_sha256, tenant_id, person_id, label) VALUES (%s, %s, %s, 'test')",
                (hash_token(token), fx.tenant, fx.ids["sam"]),
            )
        conn.commit()
        principal = resolver.for_token(token, Surface.MCP)
        assert principal.user_id == fx.ids["sam"] and principal.level == 0
        with conn.cursor() as cur:
            cur.execute("UPDATE api_token SET revoked_at = now() WHERE token_sha256 = %s", (hash_token(token),))
        conn.commit()
        from agents.gate.resolve import NotAuthorized

        with pytest.raises(NotAuthorized):
            resolver.for_token(token, Surface.MCP)
    finally:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM api_token WHERE token_sha256 = %s", (hash_token(token),))
        conn.commit()
        conn.close()


def test_live_graph_rejects_writes(live):
    _, graph, _, _ = live
    with pytest.raises(Exception, match="read-only"):
        graph.ro_query("CREATE (:Scratch {x: 1})")
