"""Seed the fake company into FalkorDB ``co:{tenant}`` and Postgres.

Usage: python -m scripts.seed_fixture [--reset] [--skip-graph] [--skip-postgres] [--skip-embeddings]

Only for the fixture tenant: the Postgres seed replaces that tenant's
memberships, sync times and chunks.
"""

import argparse
import os

from fixtures.acme import build
from fixtures.seed import apply_migrations, embed_texts, seed_graph, seed_postgres
from shared.config import load_config
from shared.env import load_env
from shared.schema import company_graph


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reset", action="store_true", help="delete the company graph first")
    parser.add_argument("--skip-graph", action="store_true")
    parser.add_argument("--skip-postgres", action="store_true")
    parser.add_argument("--skip-embeddings", action="store_true")
    args = parser.parse_args()

    load_env()
    cfg = load_config()
    tenant = os.environ.get("WEAVE_TENANT", cfg.tenant)
    if tenant != cfg.tenant:
        raise SystemExit(f"refusing to seed {tenant!r}; the fixture replaces tenant data, only {cfg.tenant!r}")
    fx = build(tenant, cfg)

    if not args.skip_graph:
        from shared.graphdb import connect

        db = connect()
        name = company_graph(tenant)
        if args.reset and name in db.list_graphs():
            db.select_graph(name).delete()
        counts = seed_graph(db.select_graph(name), fx)
        print(f"graph={name} nodes={counts['nodes']} edges={counts['edges']}")

    if args.skip_postgres:
        return
    from shared.pg import connect_pg

    direct = connect_pg(direct=True)
    try:
        applied = apply_migrations(direct)
    finally:
        direct.close()
    print(f"migrations={','.join(applied)} applied (direct connection)")

    embeddings = None
    if not args.skip_embeddings:
        embeddings = embed_texts([c["text"] for c in fx.chunks], cfg.embedding.model, cfg.embedding.dim)
    conn = connect_pg()
    try:
        pg = seed_postgres(conn, fx, embeddings)
    finally:
        conn.close()
    print(" ".join(f"{key}={value}" for key, value in pg.items()) + f" embedded={embeddings is not None}")


if __name__ == "__main__":
    main()
