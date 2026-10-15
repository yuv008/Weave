"""Load ``.env.local`` then ``.env`` into the process environment if they exist.

Existing values win, so ``.env.local`` (written by ``neon env pull``) takes
precedence over ``.env``.
"""

import os
from pathlib import Path


def load_env(*paths: str | Path) -> None:
    for path in paths or (".env.local", ".env"):
        env_file = Path(path)
        if not env_file.exists():
            continue
        for line in env_file.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, value = stripped.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
