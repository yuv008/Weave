"""Re-stamp the fixture tenant's ACL sync times so the stale-source rule keeps it visible.

Usage: python -m scripts.touch_sync [--tenant acme] [--every SECONDS]

``--every`` keeps re-stamping until stopped (Ctrl+C), for live demos longer
than the stale window. Only for the fake tenant: real tenants get sync times
from the connectors.
"""

import argparse
import os
import time

from shared.config import load_config
from shared.env import load_env
from shared.pg import connect_pg


def touch(tenant: str) -> int:
    conn = connect_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("UPDATE source_sync SET last_good_sync_at = now() WHERE tenant_id = %s", (tenant,))
            touched = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return touched


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tenant")
    parser.add_argument("--every", type=int, metavar="SECONDS")
    args = parser.parse_args()

    load_env()
    cfg = load_config()
    tenant = args.tenant or os.environ.get("WEAVE_TENANT", cfg.tenant)
    if tenant != cfg.tenant:
        raise SystemExit(f"refusing to touch sync times for {tenant!r}; only the fixture tenant {cfg.tenant!r}")
    if args.every is not None and not 0 < args.every < cfg.acl_stale_minutes * 60:
        raise SystemExit(f"--every must be between 1 and {cfg.acl_stale_minutes * 60 - 1} seconds")

    while True:
        print(f"tenant={tenant} sources_touched={touch(tenant)}", flush=True)
        if args.every is None:
            return
        time.sleep(args.every)


if __name__ == "__main__":
    main()
