"""Smoke test: the active decision on a topic, with only the evidence a person may see.

Usage: python -m scripts.query_active_decision [--as sam] [--topic SSO]

Groups come from the fixture memberships here; the real gate will read
them from Postgres.
"""

import argparse
import os

from fixtures.acme import build
from shared.acl import ORG_WIDE
from shared.config import load_config
from shared.env import load_env
from shared.graphdb import open_graph
from shared.ids import entity_id
from shared.schema import Label, Prop, Rel, Status

ACTIVE_DECISION = f"""
MATCH (d:{Label.DECISION} {{{Prop.STATUS}: $active}})-[:{Rel.ABOUT}]->(t:{Label.TOPIC} {{{Prop.ID}: $topic}})
MATCH (d)-[e:{Rel.EVIDENCED_BY}]->(i:{Label.ITEM})
WHERE i.{Prop.MIN_LEVEL} <= $level
  AND ANY(g IN i.{Prop.ACL_GROUPS} WHERE g IN $groups)
OPTIONAL MATCH (i)-[:{Rel.AUTHORED_BY}]->(author:{Label.PERSON})
OPTIONAL MATCH (d)-[:{Rel.SUPERSEDES}]->(old:{Label.DECISION})
WITH d, e, i, author, old,
     [(old)-[:{Rel.EVIDENCED_BY}]->(oi:{Label.ITEM})
      WHERE oi.{Prop.MIN_LEVEL} <= $level AND ANY(g IN oi.{Prop.ACL_GROUPS} WHERE g IN $groups) | oi] AS old_evidence
RETURN d.{Prop.ID}, d.{Prop.STATEMENT}, d.{Prop.VALID_FROM},
       CASE WHEN size(old_evidence) > 0 THEN old.{Prop.STATEMENT} END,
       collect({{quote: e.{Prop.QUOTE}, author: author.{Prop.NAME}, url: i.{Prop.SOURCE_URL}, at: i.{Prop.OCCURRED_AT}}}) AS evidence
ORDER BY d.{Prop.VALID_FROM} DESC
LIMIT 1
"""


def principal(fx, who: str) -> tuple[int, list[str]]:
    person = fx.node(fx.ids[who])
    groups = sorted({m["acl_group"] for m in fx.memberships if m["person_id"] == person.id} | {ORG_WIDE})
    return person.props[Prop.LEVEL], groups


def active_decision(graph, topic_id: str, level: int, groups: list[str]):
    rows = graph.ro_query(
        ACTIVE_DECISION,
        {"active": Status.ACTIVE, "topic": topic_id, "level": level, "groups": groups},
    ).result_set
    return rows[0] if rows else None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as", dest="who", default="sam")
    parser.add_argument("--topic", default="SSO")
    args = parser.parse_args()

    load_env()
    cfg = load_config()
    tenant = os.environ.get("WEAVE_TENANT", cfg.tenant)
    fx = build(tenant, cfg)
    level, groups = principal(fx, args.who)
    row = active_decision(open_graph(tenant), entity_id(tenant, Label.TOPIC, args.topic), level, groups)
    if row is None:
        print(f"No active decision on {args.topic} visible to {args.who}. Try search_context.")
        return
    decision_id, statement, valid_from, replaced, evidence = row
    print(f"{statement}  (active since {valid_from}, id {decision_id})")
    if replaced:
        print(f"  replaces: {replaced}")
    for entry in evidence:
        print(f"  - \"{entry['quote']}\" by {entry['author']} at {entry['at']}  {entry['url']}")


if __name__ == "__main__":
    main()
