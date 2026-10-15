"""Smoke test for the Postgres contract: memberships, outbox, and a filtered vector search.

Usage: python -m scripts.query_chunks [--as sam] [--question "..."]

The ACL filter runs in SQL before ranking, as the architecture requires.
The caller's level comes from the fixture; groups come from Postgres.
"""

import argparse
import os

from fixtures.acme import build
from fixtures.seed import embed_texts, vector_literal
from shared.acl import ORG_WIDE
from shared.config import load_config
from shared.env import load_env
from shared.pg import connect_pg
from shared.schema import Prop

SEARCH = """
SELECT item_id, left(text, 90), 1 - (embedding <=> %s::vector) AS similarity
FROM chunk
WHERE tenant_id = %s
  AND embedding IS NOT NULL
  AND min_level <= %s
  AND acl_groups && %s::text[]
ORDER BY embedding <=> %s::vector
LIMIT 3
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--as", dest="who", default="sam")
    parser.add_argument("--question", default="Which SSO provider did engineering choose?")
    parser.add_argument("--skip-search", action="store_true", help="skip the embedding and vector search")
    args = parser.parse_args()

    load_env()
    cfg = load_config()
    tenant = os.environ.get("WEAVE_TENANT", cfg.tenant)
    person = build(tenant, cfg).node_by_key(args.who)
    level = person.props[Prop.LEVEL]

    conn = connect_pg()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT acl_group FROM group_membership WHERE tenant_id = %s AND person_id = %s ORDER BY acl_group",
                (tenant, person.id),
            )
            groups = [row[0] for row in cur.fetchall()] + [ORG_WIDE]
            print(f"caller={args.who} level={level} groups={groups}")

            cur.execute(
                "SELECT kind, fact_id, new_fact_id FROM change_event WHERE tenant_id = %s AND NOT processed",
                (tenant,),
            )
            for kind, fact, new_fact in cur.fetchall():
                print(f"pending_change_event kind={kind} fact={fact} new_fact={new_fact}")

            cur.execute("SELECT acl_epoch FROM tenant_acl WHERE tenant_id = %s", (tenant,))
            print(f"acl_epoch={cur.fetchone()[0]}")

            cur.execute(
                """
                SELECT count(*), count(*) FILTER (WHERE min_level <= %s AND acl_groups && %s::text[])
                FROM chunk WHERE tenant_id = %s
                """,
                (level, groups, tenant),
            )
            total, visible = cur.fetchone()
            print(f"chunks_total={total} chunks_visible_to_{args.who}={visible}")
            if args.skip_search:
                return

            vector = vector_literal(embed_texts([args.question], cfg.embedding.model, cfg.embedding.dim)[0])
            cur.execute(SEARCH, (vector, tenant, level, groups, vector))
            print(f"question={args.question!r}")
            for item_id, text, similarity in cur.fetchall():
                print(f"  {similarity:.3f}  {item_id}  {text}")
    finally:
        conn.close()


if __name__ == "__main__":
    main()
