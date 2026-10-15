"""Check that FalkorDB and Postgres from ``.env.local`` / ``.env`` are reachable. Prints no secrets.

Usage: python -m scripts.check_stores [--skip-graph]
"""

import argparse
import sys

from shared.env import load_env


def _check_pg(direct: bool) -> bool:
    from shared.pg import connect_pg

    label = "postgres_direct" if direct else "postgres_pooled"
    try:
        conn = connect_pg(direct=direct)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database(), split_part(version(), ' ', 2)")
                database, version = cur.fetchone()
                cur.execute("SELECT 1 FROM pg_available_extensions WHERE name = 'vector'")
                has_vector = cur.fetchone() is not None
        finally:
            conn.close()
    except Exception as exc:
        print(f"{label}=failed error={type(exc).__name__}")
        return False
    print(f"{label}=ok database={database} postgres={version} pgvector_available={has_vector}")
    return has_vector


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--skip-graph", action="store_true")
    args = parser.parse_args()

    load_env()
    ok = _check_pg(direct=False) & _check_pg(direct=True)

    if not args.skip_graph:
        try:
            from shared.graphdb import connect

            graphs = connect().list_graphs()
            print(f"falkordb=ok graphs={sorted(graphs)}")
        except Exception as exc:
            ok = False
            print(f"falkordb=failed error={type(exc).__name__}")

    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
