"""Run the MCP server.

Usage: python -m agents.mcp [--host 127.0.0.1] [--port 8765]

Clients send ``Authorization: Bearer <token>``; tokens come from
``scripts/issue_demo_tokens.py`` (or the web app later).
"""

import argparse
import logging

import uvicorn

from agents.gate.resolve import Resolver
from agents.gate.stores import PostgresGateStore
from agents.graph import open_company
from agents.mcp.audit import PostgresAudit
from agents.mcp.server import TenantDirectory, build_app
from shared.config import load_config
from shared.env import load_env
from shared.graphdb import connect
from shared.pg import connect_pg


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    load_env()
    cfg = load_config()
    db = connect()

    def open_graph(tenant: str):
        return open_company(tenant, db)

    resolver = Resolver(
        PostgresGateStore(connect_pg),
        TenantDirectory(open_graph),
        stale_minutes=cfg.acl_stale_minutes,
    )
    app = build_app(
        resolver, open_graph, cfg, pg_connect=connect_pg, audit=PostgresAudit(connect_pg), host=args.host
    )
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
