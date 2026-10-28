"""One-time codes for the pet installer.

The downloaded program is the same file for everyone. The code travels in the
download's file name, and the installer trades it for the viewer token once.
"""

import hashlib
import threading
import time

from app.connect.secrets import decrypt, encrypt

TTL_SECONDS = 15 * 60


class SetupCodes:
    def __init__(self):
        self._rows: dict[str, tuple[str, float]] = {}
        self._lock = threading.Lock()

    def put(self, code: str, payload: dict, ttl: int = TTL_SECONDS) -> None:
        digest = _digest(code)
        blob = encrypt(
            {
                "cue_url": str(payload["cue_url"]),
                "token": str(payload["token"]),
                "mcp_url": str(payload["mcp_url"]),
            }
        )
        with self._lock:
            now = time.time()
            self._purge(now)
            self._rows[digest] = (blob, now + ttl)

    def take(self, code: str) -> dict | None:
        digest = _digest(code)
        with self._lock:
            row = self._rows.pop(digest, None)
            self._purge(time.time())
        if row is None:
            return None
        blob, expires = row
        if expires <= time.time():
            return None
        payload = decrypt(blob)
        if not payload.get("token") or not payload.get("cue_url") or not payload.get("mcp_url"):
            return None
        return payload

    def _purge(self, now: float) -> None:
        dead = [key for key, (_, expires) in self._rows.items() if expires <= now]
        for key in dead:
            del self._rows[key]


def _digest(code: str) -> str:
    return hashlib.sha256(code.encode("utf-8")).hexdigest()
