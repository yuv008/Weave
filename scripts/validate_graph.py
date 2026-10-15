"""Check a ``co:{tenant}`` graph against the shared contract.

Usage: python -m scripts.validate_graph [--tenant acme] [--fixture]
``--fixture`` checks the in-memory fixture without a database.
Exit code 1 when any check fails.
"""

import argparse
import os
import sys

from shared.config import load_config
from shared.env import load_env
from shared.validate import EdgeRow, NodeRow, export_graph, validate


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant")
    parser.add_argument("--fixture", action="store_true")
    args = parser.parse_args()

    load_env()
    cfg = load_config()
    tenant = args.tenant or os.environ.get("WEAVE_TENANT", cfg.tenant)

    if args.fixture:
        from fixtures.acme import build

        fx = build(tenant, cfg)
        nodes = [NodeRow(n.labels, n.props) for n in fx.nodes]
        edges = [EdgeRow(e.rel, e.src, e.dst, e.props) for e in fx.edges]
    else:
        from shared.graphdb import open_graph

        nodes, edges = export_graph(open_graph(tenant))

    errors = validate(tenant, nodes, edges)
    print(f"nodes={len(nodes)} edges={len(edges)} errors={len(errors)}")
    for error in errors:
        print(f"  {error}")
    sys.exit(1 if errors else 0)


if __name__ == "__main__":
    main()
