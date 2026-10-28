"""Take ``org:all`` off items from private GitHub repositories that are kept to the company owners.

The worker narrows a private repository's ``connected_source`` row, but an item keeps
its old ``acl_groups`` until GitHub sends a new version of it. Run this once after the
worker has polled, from the owner connection. Prints counts only.

Usage: python -m scripts.restrict_private_repos [--dry-run]
"""

import argparse

from shared.acl import ORG_WIDE
from shared.env import load_env
from shared.schema import Prop

_COUNT = f"MATCH (n) WHERE $group IN n.{Prop.ACL_GROUPS} AND $org IN n.{Prop.ACL_GROUPS} RETURN count(n)"
_NARROW = f"""
MATCH (n) WHERE $group IN n.{Prop.ACL_GROUPS} AND $org IN n.{Prop.ACL_GROUPS}
SET n.{Prop.ACL_GROUPS} = [g IN n.{Prop.ACL_GROUPS} WHERE g <> $org]
RETURN count(n)
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env()
    from app.graph.client import connect_graph
    from shared.pg import connect_pg

    conn = connect_pg(direct=True)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT tenant_id, acl_group FROM connected_source
                WHERE acl_group LIKE 'github:%%' AND NOT org_wide
                ORDER BY tenant_id, acl_group
                """
            )
            rows = cur.fetchall()
        conn.commit()
    finally:
        conn.close()

    for tenant, group in rows:
        graph = connect_graph(tenant=tenant)
        params = {"group": group, "org": ORG_WIDE}
        query = _COUNT if args.dry_run else _NARROW
        found = graph.query(query, params).result_set
        count = found[0][0] if found else 0
        print(f"tenant={tenant} group={group} {'would_narrow' if args.dry_run else 'narrowed'}={count}")
    print(f"private_repos={len(rows)}")


if __name__ == "__main__":
    main()
