"""Claiming ``change_event`` rows (``migrations/001_contract.sql``), one transaction per event.

``claim`` is a context manager that yields the oldest pending event for a
tenant, or ``None``. A normal exit marks it processed; an exception leaves it
pending for the next tick. In Postgres the row is held with ``FOR UPDATE SKIP
LOCKED`` for the whole block, so concurrent workers take different rows and
never wait on each other. ``change_event`` is shared contract and is never
altered; retries stay safe because the tasks a worker writes have
deterministic ids.
"""

import threading
from collections.abc import Callable, Collection, Iterable, Iterator
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import partial
from typing import Protocol

from shared.pg import connect_pg, use_tenant


@dataclass(frozen=True)
class ChangeEvent:
    id: str
    tenant: str
    fact_id: str
    kind: str
    new_fact_id: str | None = None
    created_at: datetime | str | None = None

    @property
    def change(self) -> str:
        """The change id ``impact_of`` takes: ``{fact_id}:{kind}``."""

        return f"{self.fact_id}:{self.kind}"


class Outbox(Protocol):
    def claim(self, tenant: str, *, skip: Collection[str] = ()) -> AbstractContextManager[ChangeEvent | None]:
        """The oldest pending event not in ``skip``; processed on a normal exit, left pending on an error."""
        ...


_CLAIM = """
SELECT id, tenant_id, fact_id, kind, new_fact_id, created_at FROM change_event
WHERE tenant_id = %s AND NOT processed AND NOT (id = ANY(%s::text[]))
ORDER BY created_at LIMIT 1 FOR UPDATE SKIP LOCKED
"""


class PostgresOutbox:
    """Neon over the direct (unpooled) endpoint: the row lock lives for the whole transaction."""

    def __init__(self, connect: Callable | None = None):
        self._connect = connect or partial(connect_pg, direct=True)

    @contextmanager
    def claim(self, tenant: str, *, skip: Collection[str] = ()) -> Iterator[ChangeEvent | None]:
        conn = self._connect()
        try:
            use_tenant(conn, tenant)
            with conn.cursor() as cur:
                cur.execute(_CLAIM, (tenant, list(skip)))
                row = cur.fetchone()
            if row is None:
                conn.rollback()
                yield None
                return
            event = ChangeEvent(*row)
            try:
                yield event
            except BaseException:
                conn.rollback()
                raise
            with conn.cursor() as cur:
                cur.execute("UPDATE change_event SET processed = true WHERE id = %s", (event.id,))
            conn.commit()
        finally:
            conn.close()


class MemoryOutbox:
    """For tests. Same claim semantics, in process."""

    def __init__(self, events: Iterable[ChangeEvent] = ()):
        self._lock = threading.Lock()
        self.events: dict[str, ChangeEvent] = {e.id: e for e in events}
        self.processed: set[str] = set()
        self._claimed: set[str] = set()

    @classmethod
    def from_fixture(cls, fx) -> "MemoryOutbox":
        """The fixture's ``change_events`` rows, with their ``processed`` flags."""

        outbox = cls(
            ChangeEvent(e["id"], fx.tenant, e["fact_id"], e["kind"], e.get("new_fact_id"), e.get("created_at"))
            for e in fx.change_events
        )
        outbox.processed |= {e["id"] for e in fx.change_events if e.get("processed")}
        return outbox

    def add(self, event: ChangeEvent) -> None:
        with self._lock:
            self.events[event.id] = event
            self.processed.discard(event.id)

    def pending(self, tenant: str) -> list[ChangeEvent]:
        with self._lock:
            return self._pending(tenant, ())

    def _pending(self, tenant: str, skip: Collection[str]) -> list[ChangeEvent]:
        return sorted(
            (e for e in self.events.values()
             if e.tenant == tenant and e.id not in self.processed and e.id not in skip),
            key=lambda e: str(e.created_at or ""),
        )

    @contextmanager
    def claim(self, tenant: str, *, skip: Collection[str] = ()) -> Iterator[ChangeEvent | None]:
        with self._lock:
            free = [e for e in self._pending(tenant, skip) if e.id not in self._claimed]
            event = free[0] if free else None
            if event is not None:
                self._claimed.add(event.id)
        if event is None:
            yield None
            return
        try:
            yield event
            with self._lock:
                self.processed.add(event.id)
        finally:
            with self._lock:
                self._claimed.discard(event.id)
