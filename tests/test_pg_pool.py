"""Unit tests for the process-local Postgres pool wrapper."""

from unittest.mock import MagicMock

from shared.pg import _PooledConn, connect_pg


class _FakePool:
    def __init__(self):
        self.puts: list[object] = []

    def putconn(self, conn):
        self.puts.append(conn)


class _FakeConn:
    def __init__(self, *, rollback_raises=False):
        self.autocommit = False
        self.rollback_raises = rollback_raises
        self.closed = False
        self.rollback_calls = 0

    def rollback(self):
        self.rollback_calls += 1
        if self.rollback_raises:
            raise RuntimeError("broken")

    def close(self):
        self.closed = True


def test_pooled_close_returns_connection_and_clears_autocommit():
    pool = _FakePool()
    inner = _FakeConn()
    inner.autocommit = True
    wrapped = _PooledConn(inner, pool)
    wrapped.close()
    assert inner.autocommit is False
    assert inner.rollback_calls == 1
    assert pool.puts == [inner]


def test_pooled_close_returns_connection_when_rollback_fails():
    pool = _FakePool()
    inner = _FakeConn(rollback_raises=True)
    wrapped = _PooledConn(inner, pool)
    wrapped.close()
    assert pool.puts == [inner]


def test_connect_pg_with_url_bypasses_pool(monkeypatch):
    seen = {}

    def fake_connect(dsn, **kwargs):
        seen["dsn"] = dsn
        seen["kwargs"] = kwargs
        return object()

    monkeypatch.setattr("psycopg.connect", fake_connect)
    conn = connect_pg(url="postgresql://test/db")
    assert seen["dsn"] == "postgresql://test/db"
    assert not isinstance(conn, _PooledConn)
