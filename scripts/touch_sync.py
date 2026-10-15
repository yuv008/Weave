"""Re-stamp the fixture tenant's ACL sync times so the stale-source rule keeps it visible.

Usage: python -m scripts.touch_sync [--tenant acme]

Only for the fake tenant: real tenants get sync times from the connectors.
"""

import argparse
import os

from shared.config import load_config
from shared.env import load_env
from shared.pg import connect_pg


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant")
    args = parser.parse_args()

    load_env()
    cfg = load_config()
    tenant = args.tenant or os.environ.get("WEAVE_TENANT", cfg.tenant)
    if tenant != cfg.tenant:
        raise SystemExit(f"refusing to touch sync times for {tenant!r}; only the fixture tenant {cfg.tenant!r}")
    conn = connect_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE source_sync SET last_good_sync_at = now() WHERE tenant_id = %s", (tenant,))
            touched = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    print(f"tenant={tenant} sources_touched={touched}")


if __name__ == "__main__":
    main()
