"""Demo bearer tokens for the CLI scripts: ``WEAVE_TOKEN_<WHO>`` from the environment or ``.env.tokens``.

Tokens are never printed.
"""

import os
from pathlib import Path

TOKENS = Path(__file__).resolve().parent.parent / ".env.tokens"


def token_for(who: str) -> str:
    key = f"WEAVE_TOKEN_{who.upper()}"
    if os.environ.get(key):
        return os.environ[key]
    if TOKENS.exists():
        for line in TOKENS.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == key and value.strip():
                return value.strip()
    raise SystemExit(f"{key} not set; run: python -m scripts.issue_demo_tokens > .env.tokens")
