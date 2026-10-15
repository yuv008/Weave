from datetime import UTC, datetime, timedelta, timezone

import pytest

from app.models.canonical import conversation_node_id, message_node_id
from app.models.canonical_document import document_node_id
from shared.acl import ORG_WIDE, acl_group, is_acl, parse_acl
from shared.config import load_config
from shared.ids import claim_id, document_id, entity_id, person_id, slack_channel_id, slack_message_id
from shared.indexes import index_statements
from shared.schema import Label, company_graph, private_graph
from shared.timefmt import is_ts, to_ts


def test_ids_match_existing_connector_formats():
    assert slack_message_id("acme", "C1", "1700000000.000100") == message_node_id("acme", "C1:1700000000.000100")
    assert slack_channel_id("acme", "T1", "C1") == conversation_node_id("acme", "T1", "C1")
    assert document_id("acme", "notion", "W1", "page") == document_node_id("acme", "notion", "W1", "page")


def test_claim_ids_are_deterministic_and_normalized():
    first = claim_id("acme", Label.DECISION, "acme:slack:message:C1:1.0", "Use Provider A for SSO.")
    again = claim_id("acme", Label.DECISION, "acme:slack:message:C1:1.0", "  use provider A   for SSO. ")
    other = claim_id("acme", Label.DECISION, "acme:slack:message:C1:2.0", "Use Provider A for SSO.")
    assert first == again
    assert first != other
    assert first.startswith("acme:decision:")


def test_entity_and_person_ids():
    assert entity_id("acme", Label.TOPIC, " Single  Sign-On ") == "acme:topic:single-sign-on"
    assert person_id("acme", " Priya@Example.com ") == "acme:person:priya@example.com"
    with pytest.raises(ValueError):
        person_id("acme", "U0PRIYA")


def test_acl_round_trip_and_rejects():
    assert parse_acl(acl_group("slack", "C123")) == ("slack", "C123")
    assert parse_acl(acl_group("github", "org/repo")) == ("github", "org/repo")
    assert parse_acl(ORG_WIDE) == ("org", "all")
    for bad in ("slack", "slack:", "jira:ABC", "slack: C1", "Slack:C1", "org:everyone", ""):
        assert not is_acl(bad), bad


def test_timestamps_are_utc_seconds():
    ist = timezone(timedelta(hours=5, minutes=30))
    value = to_ts(datetime(2026, 10, 12, 15, 0, 0, 123456, tzinfo=ist))
    assert value == "2026-10-12T09:30:00+00:00"
    assert is_ts(value)
    assert not is_ts("2026-10-12T09:30:00.123456+00:00")
    assert not is_ts("2026-10-12T09:30:00")
    assert to_ts(datetime(2026, 1, 1)) == "2026-01-01T00:00:00+00:00"
    assert to_ts(datetime(2026, 1, 1, tzinfo=UTC)) < to_ts(datetime(2026, 1, 2, tzinfo=UTC))


def test_graph_names():
    assert company_graph("acme") == "co:acme"
    assert private_graph("acme", "sam") == "u:acme:sam"


def test_config_loads_contract_values():
    cfg = load_config()
    assert cfg.embedding.model == "BAAI/bge-small-en-v1.5"
    assert cfg.embedding.dim == 384
    assert cfg.sources["slack:C0LEADERS"].min_level == 2
    assert cfg.sources["slack:C0LEADERS"].cascade_policy == "broadcast"
    assert cfg.authority == {"notion": 3, "gdrive": 3, "github": 2, "slack": 1}


def test_index_statements_cover_every_contract_label():
    statements = "\n".join(index_statements())
    for label in (Label.DECISION, Label.ITEM, Label.SOURCE, Label.PERSON, Label.PROJECTION):
        assert f"(n:{label}) ON (n.id)" in statements
