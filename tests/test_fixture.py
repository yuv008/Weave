import copy
import os
import uuid

import pytest

from fixtures.acme import build
from shared.acl import ORG_WIDE, is_acl
from shared.schema import Label, ProjectionStatus, Prop, Rel, Status
from shared.validate import EdgeRow, NodeRow, validate


@pytest.fixture(scope="module")
def fx():
    return build("acme")


def _rows(fx):
    nodes = [NodeRow(n.labels, copy.deepcopy(n.props)) for n in fx.nodes]
    edges = [EdgeRow(e.rel, e.src, e.dst, copy.deepcopy(e.props)) for e in fx.edges]
    return nodes, edges


def test_fixture_passes_contract(fx):
    nodes, edges = _rows(fx)
    assert validate("acme", nodes, edges) == []


def test_fixture_is_deterministic():
    first, second = build("acme"), build("acme")
    assert [n.id for n in first.nodes] == [n.id for n in second.nodes]
    assert first.ids == second.ids


def test_fixture_cast_and_sources(fx):
    people = [n for n in fx.nodes if n.label == Label.PERSON]
    sources = [n for n in fx.nodes if n.label == Label.SOURCE]
    assert sorted(p.props[Prop.NAME] for p in people) == ["Dev", "Maya", "Priya", "Rohan", "Sam"]
    assert len(sources) == 5
    person_ids = {p.id for p in people}
    for membership in fx.memberships:
        assert membership["person_id"] in person_ids
        assert is_acl(membership["acl_group"])


def test_supersession_story(fx):
    d1, d2 = fx.node(fx.ids["decision_provider_a"]), fx.node(fx.ids["decision_provider_b"])
    assert d1.props[Prop.STATUS] == Status.SUPERSEDED
    assert d2.props[Prop.STATUS] == Status.ACTIVE
    assert d1.props[Prop.VALID_TO] == d2.props[Prop.VALID_FROM]
    edges = {(e.rel, e.src, e.dst) for e in fx.edges}
    assert (Rel.SUPERSEDES, d2.id, d1.id) in edges
    assert (Rel.DEPENDS_ON, fx.ids["commitment_acme_sso"], d1.id) in edges
    assert fx.change_events == [
        {
            "id": f"{d1.id}:superseded",
            "fact_id": d1.id,
            "kind": "superseded",
            "new_fact_id": d2.id,
            "created_at": "2026-10-12T09:31:00+00:00",
            "processed": False,
        }
    ]


def test_projections(fx):
    approved = fx.node(fx.ids["projection_q4_priority"])
    draft = fx.node(fx.ids["projection_budget_draft"])
    assert approved.props[Prop.STATUS] == ProjectionStatus.APPROVED
    assert approved.props[Prop.AUDIENCE] == [ORG_WIDE]
    assert draft.props[Prop.STATUS] == ProjectionStatus.DRAFT
    budget_items = [e.dst for e in fx.edges if e.rel == Rel.EVIDENCED_BY and e.src == fx.ids["figure_q4_budget"]]
    assert all(fx.node(i).props[Prop.MIN_LEVEL] == 2 for i in budget_items)


def test_evidence_spans_point_at_the_quote(fx):
    texts = {c["item_id"]: c["text"] for c in fx.chunks}
    for edge in fx.edges:
        if edge.rel == Rel.EVIDENCED_BY:
            start, end = edge.props[Prop.SPAN]
            assert texts[edge.dst][start:end] == edge.props[Prop.QUOTE]


def test_validator_catches_missing_evidence(fx):
    nodes, edges = _rows(fx)
    d2 = fx.ids["decision_provider_b"]
    edges = [e for e in edges if not (e.rel == Rel.EVIDENCED_BY and e.src == d2)]
    assert f"Decision {d2}: no EVIDENCED_BY" in validate("acme", nodes, edges)


def test_validator_catches_bad_values(fx):
    nodes, edges = _rows(fx)
    for node in nodes:
        if node.props[Prop.ID] == fx.ids["decision_provider_a"]:
            node.props[Prop.STATUS] = "replaced"
        if node.props[Prop.ID] == fx.ids["item_deal_promise"]:
            node.props[Prop.ACL_GROUPS] = ["slack C0DEALACME"]
        if node.props[Prop.ID] == fx.ids["decision_provider_b"]:
            node.props["validTo"] = node.props.pop(Prop.VALID_FROM)
    errors = "\n".join(validate("acme", nodes, edges))
    assert "status 'replaced'" in errors
    assert "bad acl string 'slack C0DEALACME'" in errors
    assert "missing valid_from" in errors
    assert "superseded by" in errors


def test_validator_catches_wrong_edge_direction(fx):
    nodes, edges = _rows(fx)
    item, person = fx.ids["item_deal_promise"], fx.ids["priya"]
    edges.append(EdgeRow(Rel.AUTHORED_BY, person, item, {}))
    assert any("AUTHORED_BY Person -> Item" in e for e in validate("acme", nodes, edges))


@pytest.fixture
def live_graph():
    pytest.importorskip("falkordb")
    from shared.graphdb import connect

    try:
        if os.environ.get("FALKORDB_URL", "").strip():
            client = connect()
        else:
            client = connect(
                os.environ.get("FALKORDB_HOST", "localhost"),
                int(os.environ.get("FALKORDB_PORT", "6380")),
            )
        graph = client.select_graph(f"co:test_{uuid.uuid4().hex}")
        graph.query("RETURN 1")
    except Exception as exc:
        pytest.skip(f"FalkorDB is not available: {exc}")
    yield graph
    graph.delete()


def test_seed_twice_then_query_active_decision(live_graph, fx):
    from fixtures.seed import seed_graph
    from scripts.query_active_decision import active_decision, principal
    from shared.validate import export_graph

    seed_graph(live_graph, fx)
    seed_graph(live_graph, fx)
    nodes, edges = export_graph(live_graph)
    assert len(nodes) == len(fx.nodes)
    assert len(edges) == len(fx.edges)
    assert validate("acme", nodes, edges) == []

    level, groups = principal(fx, "sam")
    row = active_decision(live_graph, fx.ids["topic_sso"], level, groups)
    assert row[0] == fx.ids["decision_provider_b"]
    assert row[3] == "Use Provider A for SSO."
    assert {e["author"] for e in row[4]} == {"Dev"}

    level, groups = principal(fx, "rohan")
    assert active_decision(live_graph, fx.ids["topic_sso"], level, groups) is None
