"""Delete a source's stored data, or GitHub posts written by bots.

``--group`` removes everything one company stored from one source: its items,
placeholders, and source node, their chunks, sync checkpoints and failures,
freshness row, and memberships. People, customers, and deals left with no
links are removed too, so a correspondent's name and email do not outlive the
messages that named them. A person with a level (a member) is kept. Take the
source out of ``config/weave.yaml`` or disconnect it first, or the next poll
writes it back. ``--github-bots`` removes GitHub items whose author is a bot.
Prints counts only.

Usage:
  python -m scripts.prune_source --tenant real --group gmail:someone@example.com [--dry-run]
  python -m scripts.prune_source --tenant pict --github-bots [--dry-run]
"""

import argparse

from shared.acl import parse_acl
from shared.env import load_env
from shared.schema import Prop

_BY_GROUP = f"MATCH (n) WHERE $group IN n.{Prop.ACL_GROUPS}"
_BOTS = f"MATCH (n:Item) WHERE n.{Prop.SOURCE} = 'github' AND coalesce(n.author_external_id, '') ENDS WITH '[bot]'"
_LINKED = f"""
MATCH (p) WHERE (p:Person OR p:Customer OR p:Deal) AND p.{Prop.TENANT_ID} = $tenant AND p.{Prop.LEVEL} IS NULL
OPTIONAL MATCH (p)--(x)
RETURN p.{Prop.ID}, collect(x.{Prop.ID})
"""
_DELETE = f"UNWIND $ids AS id MATCH (n {{{Prop.ID}: id}}) DETACH DELETE n"


def prune(tenant: str, graph, cur, *, group: str | None = None, bots: bool = False, dry_run: bool) -> dict:
    """Postgres deletes run first and commit after the graph deletes, so a failure part way leaves nothing half done."""

    if bool(group) == bots:
        raise ValueError("give exactly one of group or bots")
    match, params = (_BY_GROUP, {"group": group}) if group else (_BOTS, {})
    ids = [row[0] for row in graph.query(f"{match} RETURN n.{Prop.ID}", params).result_set]
    gone = set(ids)
    # People, customers, and deals whose every link points at something being deleted.
    orphans = [
        node for node, linked in graph.query(_LINKED, {"tenant": tenant}).result_set
        if linked and all(other in gone for other in linked)
    ]
    report = {"nodes": len(ids), "orphans": len(orphans)}
    chunks = "FROM chunk WHERE tenant_id = %s AND (item_id = ANY(%s) OR %s = ANY(acl_groups))"
    chunk_args = (tenant, ids, group or "")
    cur.execute(f"SELECT count(*) {chunks}", chunk_args)
    report["chunks"] = cur.fetchone()[0]
    if group:
        system, container = parse_acl(group)
        sync = """FROM ingest_record WHERE tenant_id = %s AND kind IN ('sync_state', 'sync_failure')
                  AND split_part(id, ':', 2) = %s
                  AND (position(%s in id) > 0 OR (payload->>'owner') || '/' || (payload->>'repo') = %s)"""
        sync_args = (tenant, system, container, container)
        cur.execute(f"SELECT count(*) {sync}", sync_args)
        report["sync_records"] = cur.fetchone()[0]
        cur.execute("SELECT count(*) FROM group_membership WHERE tenant_id = %s AND acl_group = %s", (tenant, group))
        report["memberships"] = cur.fetchone()[0]
    if dry_run:
        return report
    cur.execute(f"DELETE {chunks}", chunk_args)
    if group:
        cur.execute(f"DELETE {sync}", sync_args)
        cur.execute("DELETE FROM group_membership WHERE tenant_id = %s AND acl_group = %s", (tenant, group))
        cur.execute("DELETE FROM source_sync WHERE tenant_id = %s AND acl_group = %s", (tenant, group))
        cur.execute("DELETE FROM connected_source WHERE tenant_id = %s AND acl_group = %s", (tenant, group))
        cur.execute(
            """
            INSERT INTO tenant_acl (tenant_id, acl_epoch) VALUES (%s, 1)
            ON CONFLICT (tenant_id) DO UPDATE SET acl_epoch = tenant_acl.acl_epoch + 1, updated_at = now()
            """,
            (tenant,),
        )
    graph.query(_DELETE, {"ids": ids + orphans})
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--group")
    parser.add_argument("--github-bots", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env()
    from app.graph.client import connect_graph
    from shared.pg import connect_pg

    conn = connect_pg(direct=True)
    try:
        with conn.cursor() as cur:
            report = prune(args.tenant, connect_graph(tenant=args.tenant), cur,
                           group=args.group, bots=args.github_bots, dry_run=args.dry_run)
        conn.commit()
    finally:
        conn.close()
    target = args.group or "github-bots"
    print(f"tenant={args.tenant} target={target} {'dry_run' if args.dry_run else 'deleted'} "
          + " ".join(f"{key}={value}" for key, value in report.items()))


if __name__ == "__main__":
    main()
