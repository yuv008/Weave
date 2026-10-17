"""One Slack reply per event, and one answer at a time per thread.

``claim`` is ``INSERT ... ON CONFLICT DO NOTHING``. The thread lock is held
only while that reply is produced and posted, then released, so a later
mention in the same thread still gets its own answer.
"""

import threading
from contextlib import contextmanager

from shared.pg import connect_pg, use_tenant


class MemoryReplyClaims:
    """Same claim and lock behaviour, in process, for tests."""

    def __init__(self):
        self.events: dict[str, dict] = {}
        self._guard = threading.Lock()
        self._threads: dict[tuple[str, str], threading.Lock] = {}

    def claim(self, event_id: str, tenant: str, channel_id: str, thread_ts: str) -> bool:
        with self._guard:
            if not event_id or event_id in self.events:
                return False
            self.events[event_id] = {
                "tenant": tenant, "channel_id": channel_id, "thread_ts": thread_ts, "state": "claimed",
            }
            return True

    @contextmanager
    def thread(self, channel_id: str, thread_ts: str):
        with self._guard:
            lock = self._threads.setdefault((channel_id, thread_ts), threading.Lock())
        with lock:
            yield

    def finish(self, event_id: str, state: str) -> None:
        with self._guard:
            row = self.events.get(event_id)
            if row is not None:
                row["state"] = state


class PostgresReplyClaims:
    """Neon. The advisory lock uses its own connection so it outlives other commits."""

    def __init__(self, connect):
        self._connect = connect

    def claim(self, event_id: str, tenant: str, channel_id: str, thread_ts: str) -> bool:
        if not event_id:
            return False
        conn = self._connect()
        try:
            use_tenant(conn, tenant)
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO slack_reply (event_id, tenant_id, channel_id, thread_ts, state)"
                    " VALUES (%s, %s, %s, %s, 'claimed') ON CONFLICT (event_id) DO NOTHING RETURNING event_id",
                    (event_id, tenant, channel_id, thread_ts),
                )
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
        return row is not None

    @contextmanager
    def thread(self, channel_id: str, thread_ts: str):
        conn = connect_pg(direct=True)
        key = f"{channel_id}:{thread_ts}"
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT pg_advisory_lock(hashtextextended(%s, 0))", (key,))
            yield
        finally:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT pg_advisory_unlock(hashtextextended(%s, 0))", (key,))
            finally:
                conn.close()

    def finish(self, event_id: str, state: str) -> None:
        conn = self._connect()
        try:
            with conn.cursor() as cur:
                cur.execute("UPDATE slack_reply SET state = %s WHERE event_id = %s", (state, event_id))
            conn.commit()
        finally:
            conn.close()
