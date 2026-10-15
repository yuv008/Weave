"""The Postgres outbox on Neon (WEAVE_LIVE_TESTS=1): SKIP LOCKED claims, rollback and commit.

Rows go under a throwaway tenant and are deleted afterwards.
"""

import threading
import uuid
from datetime import UTC, datetime, timedelta

import pytest

from agents.watcher.outbox import ChangeEvent, PostgresOutbox
from tests.tool_support import live

pytestmark = live


def _connect():
    from shared.pg import connect_pg

    return connect_pg()


def _pending(tenant: str) -> list[str]:
    conn = _connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM change_event WHERE tenant_id = %s AND NOT processed ORDER BY created_at",
                        (tenant,))
            return [r[0] for r in cur.fetchall()]
    finally:
        conn.close()


@pytest.fixture()
def events():
    from shared.env import load_env

    load_env()
    tenant = f"test{uuid.uuid4().hex[:12]}"
    start = datetime(2026, 10, 12, 9, 0, tzinfo=UTC)
    ids = [f"{tenant}:decision:d{i}:superseded" for i in range(6)]
    conn = _connect()
    try:
        with conn.cursor() as cur:
            for i, event_id in enumerate(ids):
                cur.execute(
                    "INSERT INTO change_event (id, tenant_id, fact_id, kind, new_fact_id, created_at)"
                    " VALUES (%s, %s, %s, 'superseded', NULL, %s)",
                    (event_id, tenant, f"{tenant}:decision:d{i}", start + timedelta(minutes=i)),
                )
        conn.commit()
    finally:
        conn.close()
    try:
        yield tenant, ids
    finally:
        conn = _connect()
        try:
            with conn.cursor() as cur:
                cur.execute("DELETE FROM change_event WHERE tenant_id = %s", (tenant,))
            conn.commit()
        finally:
            conn.close()


def test_two_connections_claim_different_rows(events):
    tenant, ids = events
    outbox = PostgresOutbox()
    with outbox.claim(tenant) as first:
        with PostgresOutbox().claim(tenant) as second:
            assert isinstance(first, ChangeEvent) and isinstance(second, ChangeEvent)
            assert (first.id, second.id) == (ids[0], ids[1])
            assert first.tenant == tenant and first.change == f"{tenant}:decision:d0:superseded"
            assert _pending(tenant) == ids
        assert _pending(tenant) == [ids[0], *ids[2:]]
    assert _pending(tenant) == ids[2:]


def test_rollback_leaves_the_row_pending_and_commit_marks_it_processed(events):
    tenant, ids = events
    outbox = PostgresOutbox()
    with pytest.raises(RuntimeError):
        with outbox.claim(tenant) as event:
            assert event.id == ids[0]
            raise RuntimeError("processing failed")
    assert _pending(tenant) == ids
    with outbox.claim(tenant, skip={ids[0]}) as event:
        assert event.id == ids[1]
    assert _pending(tenant) == [ids[0], *ids[2:]]
    with outbox.claim(tenant) as event:
        assert event.id == ids[0]
    assert _pending(tenant) == ids[2:]


def test_concurrent_workers_take_each_row_once(events):
    tenant, ids = events
    claimed, lock = [], threading.Lock()

    def worker():
        outbox = PostgresOutbox()
        while True:
            with outbox.claim(tenant) as event:
                if event is None:
                    return
                with lock:
                    claimed.append(event.id)

    threads = [threading.Thread(target=worker) for _ in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids) and len(claimed) == len(set(claimed))
    assert _pending(tenant) == []


def test_no_pending_event_yields_none(events):
    tenant, _ = events
    with PostgresOutbox().claim(f"{tenant}_none") as event:
        assert event is None
