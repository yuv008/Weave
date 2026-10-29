"""Make the worker read a company's Slack, Notion, Drive, and GitHub items again from the start.

Items stored before full-text chunking have only their excerpt in ``chunk``.
This deletes the company's sync checkpoints and blanks the stored version of
those items, so the next poll re-reads every item and the upsert writes its
full-text chunks. Nothing is deleted from the graph, and items the source no
longer returns keep their current chunks. Run it once per company after the
chunking deploy. Prints counts only.

Usage: python -m scripts.reread_sources --tenant real [--dry-run]
"""

import argparse

from shared.env import load_env
from shared.schema import Prop

SYSTEMS = ["slack", "notion", "gdrive", "github"]
_ITEMS = f"MATCH (i:Item) WHERE i.{Prop.TENANT_ID} = $tenant AND i.{Prop.SOURCE} IN $systems"
_CHECKPOINTS = "FROM ingest_record WHERE tenant_id = %s AND kind = 'sync_state' AND split_part(id, ':', 2) = ANY(%s)"


def reread(tenant: str, graph, cur, *, dry_run: bool) -> dict:
    params = {"tenant": tenant, "systems": SYSTEMS}
    if dry_run:
        items = graph.query(f"{_ITEMS} RETURN count(i)", params).result_set[0][0]
        cur.execute(f"SELECT count(*) {_CHECKPOINTS}", (tenant, SYSTEMS))
        return {"items": items, "checkpoints": cur.fetchone()[0]}
    cur.execute(f"DELETE {_CHECKPOINTS}", (tenant, SYSTEMS))
    checkpoints = cur.rowcount
    items = graph.query(f"{_ITEMS} SET i.{Prop.VERSION} = '' RETURN count(i)", params).result_set[0][0]
    return {"items": items, "checkpoints": checkpoints}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--tenant", required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    load_env()
    from app.graph.client import connect_graph
    from shared.pg import connect_pg

    conn = connect_pg(direct=True)
    try:
        with conn.cursor() as cur:
            report = reread(args.tenant, connect_graph(tenant=args.tenant), cur, dry_run=args.dry_run)
        conn.commit()
    finally:
        conn.close()
    print(f"tenant={args.tenant} {'dry_run' if args.dry_run else 'reset'} "
          + " ".join(f"{key}={value}" for key, value in report.items()))


if __name__ == "__main__":
    main()
