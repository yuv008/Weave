"""Bring stored access tags in line with each source's policy.

An org-wide source's items carry ``org:all`` so the whole company can read them.
Any other source keeps only its own group, and the company owners get a
membership for it. Items, source nodes, and Postgres chunks are all changed,
and a company whose access changed gets a new ACL epoch so cached access
refreshes. A group with no policy (a source that was removed) is left alone.
Run it after a deploy that changes a policy, from the owner connection.
Prints group names and counts only.

Usage: python -m scripts.apply_source_acl [--dry-run]
"""

import argparse

from shared.acl import ORG_WIDE
from shared.env import load_env
from shared.schema import Prop

_GROUPS = f"MATCH (n) WHERE n.{Prop.ACL_GROUPS} IS NOT NULL UNWIND n.{Prop.ACL_GROUPS} AS g RETURN DISTINCT g"
_TO_OPEN = f"MATCH (n) WHERE $group IN n.{Prop.ACL_GROUPS} AND NOT $org IN n.{Prop.ACL_GROUPS}"
_TO_NARROW = f"MATCH (n) WHERE $group IN n.{Prop.ACL_GROUPS} AND $org IN n.{Prop.ACL_GROUPS}"
_OPEN = f"SET n.{Prop.ACL_GROUPS} = n.{Prop.ACL_GROUPS} + $org"
_NARROW = f"SET n.{Prop.ACL_GROUPS} = [g IN n.{Prop.ACL_GROUPS} WHERE g <> $org]"

_CHUNKS_TO_OPEN = "tenant_id = %s AND %s = ANY(acl_groups) AND NOT %s = ANY(acl_groups)"
_CHUNKS_TO_NARROW = "tenant_id = %s AND %s = ANY(acl_groups) AND %s = ANY(acl_groups)"


def reconcile_tenant(tenant: str, graph, cur, *, dry_run: bool) -> list[dict]:
    """One row per access group in this company's graph: what changed, or would."""

    from app.graph.policy import allowed_source
    from shared.ids import person_id

    cur.execute(
        """
        SELECT u.email FROM tenant_membership AS m JOIN app_user AS u ON u.id = m.user_id
        WHERE m.tenant_id = %s AND m.role = 'owner'
        """,
        (tenant,),
    )
    owners = [person_id(tenant, email) for (email,) in cur.fetchall() if email]
    report = []
    for (group,) in graph.query(_GROUPS).result_set:
        if group == ORG_WIDE:
            continue
        system, _, container = group.partition(":")
        found = allowed_source(system, container, tenant)
        if found is None:
            report.append({"group": group, "action": "no_policy"})
            continue
        org_wide = found[1].org_wide
        match, change = (_TO_OPEN, _OPEN) if org_wide else (_TO_NARROW, _NARROW)
        params = {"group": group, "org": ORG_WIDE}
        where = _CHUNKS_TO_OPEN if org_wide else _CHUNKS_TO_NARROW
        if dry_run:
            nodes = graph.query(f"{match} RETURN count(n)", params).result_set[0][0]
            cur.execute(f"SELECT count(*) FROM chunk WHERE {where}", (tenant, group, ORG_WIDE))
            chunks = cur.fetchone()[0]
        else:
            nodes = graph.query(f"{match} {change} RETURN count(n)", params).result_set[0][0]
            update = "array_append(acl_groups, %s)" if org_wide else "array_remove(acl_groups, %s)"
            cur.execute(f"UPDATE chunk SET acl_groups = {update} WHERE {where}", (ORG_WIDE, tenant, group, ORG_WIDE))
            chunks = cur.rowcount
        members = 0
        if not org_wide:
            for owner in owners:
                if dry_run:
                    cur.execute(
                        "SELECT count(*) FROM group_membership WHERE tenant_id = %s AND person_id = %s AND acl_group = %s",
                        (tenant, owner, group),
                    )
                    members += 0 if cur.fetchone()[0] else 1
                else:
                    cur.execute(
                        """
                        INSERT INTO group_membership (tenant_id, person_id, acl_group, source_system)
                        VALUES (%s, %s, %s, %s)
                        ON CONFLICT (tenant_id, person_id, acl_group) DO NOTHING
                        """,
                        (tenant, owner, group, system),
                    )
                    members += cur.rowcount
        report.append({
            "group": group,
            "action": "open" if org_wide else "owners_only",
            "nodes": nodes,
            "chunks": chunks,
            "owner_memberships": members,
        })
    changed = any(row.get("nodes") or row.get("chunks") or row.get("owner_memberships") for row in report)
    if changed and not dry_run:
        cur.execute(
            """
            INSERT INTO tenant_acl (tenant_id, acl_epoch) VALUES (%s, 1)
            ON CONFLICT (tenant_id) DO UPDATE SET acl_epoch = tenant_acl.acl_epoch + 1, updated_at = now()
            """,
            (tenant,),
        )
    return report


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
            cur.execute("SELECT DISTINCT tenant_id FROM tenant_membership WHERE tenant_id <> 'acme' ORDER BY 1")
            tenants = [row[0] for row in cur.fetchall()]
            for tenant in tenants:
                for row in reconcile_tenant(tenant, connect_graph(tenant=tenant), cur, dry_run=args.dry_run):
                    print(f"tenant={tenant} " + " ".join(f"{key}={value}" for key, value in row.items()))
                # One company at a time: a later failure keeps the companies already done.
                if not args.dry_run:
                    conn.commit()
        conn.commit()
    finally:
        conn.close()
    print(f"companies={len(tenants)} {'dry_run' if args.dry_run else 'applied'}")


if __name__ == "__main__":
    main()
