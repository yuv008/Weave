"""Issue one API token per fixture person. Stores only hashes; prints each token once.

Usage: python -m scripts.issue_demo_tokens [--rotate]

Save the printed tokens somewhere private (for example .env.tokens, which is
gitignored). ``--rotate`` revokes the previous demo tokens first.
"""

import argparse
import os
import secrets

from agents.gate.resolve import hash_token
from fixtures.acme import build
from shared.config import load_config
from shared.env import load_env
from shared.pg import connect_pg
from shared.schema import Label, Prop

LABEL = "demo"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rotate", action="store_true")
    args = parser.parse_args()

    load_env()
    cfg = load_config()
    tenant = os.environ.get("WEAVE_TENANT", cfg.tenant)
    people = [n for n in build(tenant, cfg).nodes if n.label == Label.PERSON]

    conn = connect_pg()
    try:
        with conn.cursor() as cur:
            if args.rotate:
                cur.execute(
                    "UPDATE api_token SET revoked_at = now() WHERE tenant_id = %s AND label = %s AND revoked_at IS NULL",
                    (tenant, LABEL),
                )
            issued = []
            for person in people:
                token = f"wv_{secrets.token_urlsafe(24)}"
                cur.execute(
                    "INSERT INTO api_token (token_sha256, tenant_id, person_id, label) VALUES (%s, %s, %s, %s)",
                    (hash_token(token), tenant, person.id, LABEL),
                )
                issued.append((person.props[Prop.NAME].upper(), token))
        conn.commit()
    finally:
        conn.close()
    for name, token in issued:
        print(f"WEAVE_TOKEN_{name}={token}")


if __name__ == "__main__":
    main()
