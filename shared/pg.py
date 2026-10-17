"""Postgres connections. Uses psycopg 3, or psycopg2 if that is what is installed.

On Neon, ``DATABASE_URL`` is the pooled endpoint (PgBouncer, transaction
mode) for normal queries. ``DATABASE_URL_UNPOOLED`` is the direct endpoint;
migrations must use it. Locally both can be the same URL.

``connect_pg()`` stays the table owner. Migrations, seeds, and tests use that
and still see every row, because row level security is not forced on the
owner. Isolated app queries call ``use_tenant`` in the same transaction as
the statement. ``SET LOCAL`` and ``set_config(..., true)`` end at commit or
rollback, so a transaction-mode pooler cannot leak the role or the tenant.

Process-local pools reuse open TLS sessions to Neon. Checkouts do not ping
the server first; a failed query drops the connection from the pool.
"""

import os
import threading
from typing import Any

APP_ROLE = "weave_app"
ASSUME_APP_ROLE = "SET LOCAL ROLE weave_app"
SET_TENANT = "SELECT set_config('weave.tenant', %s, true)"

_QUERY_POOL: Any = None
_DIRECT_POOL: Any = None
_POOL_LOCK = threading.Lock()
_POOL_KWARGS = {"prepare_threshold": None}
# Neon TLS from this machine is often 2–3s on a cold socket; warm_pg() avoids that on hot paths.
_CHECKOUT_TIMEOUT = 5.0


class _PooledConn:
    """Wraps a pool checkout so ``close()`` returns the connection, not the TCP session."""

    def __init__(self, conn, pool):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_pool", pool)
        object.__setattr__(self, "_closed", False)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        conn = self._conn
        self._conn = None
        try:
            if getattr(conn, "autocommit", False):
                conn.autocommit = False
            conn.rollback()
        except Exception:
            pass
        # psycopg_pool putconn takes only the connection. A broken socket
        # comes back with an unknown transaction status and the pool replaces it.
        self._pool.putconn(conn)

    def __getattr__(self, name: str):
        return getattr(self._conn, name)

    def __setattr__(self, name: str, value) -> None:
        if name in ("_conn", "_pool", "_closed"):
            object.__setattr__(self, name, value)
        else:
            setattr(self._conn, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


def _dsn(direct: bool) -> str:
    if direct:
        dsn = os.environ.get("DATABASE_URL_UNPOOLED")
        if not dsn:
            dsn = os.environ["DATABASE_URL"]
        return dsn
    return os.environ["DATABASE_URL"]


def _pool(direct: bool):
    global _QUERY_POOL, _DIRECT_POOL
    with _POOL_LOCK:
        if direct:
            if _DIRECT_POOL is None:
                from psycopg_pool import ConnectionPool

                _DIRECT_POOL = ConnectionPool(
                    _dsn(True),
                    open=True,
                    min_size=0,
                    max_size=2,
                    timeout=_CHECKOUT_TIMEOUT,
                    kwargs=_POOL_KWARGS,
                )
            return _DIRECT_POOL
        if _QUERY_POOL is None:
            from psycopg_pool import ConnectionPool

            _QUERY_POOL = ConnectionPool(
                _dsn(False),
                open=True,
                min_size=0,
                max_size=8,
                timeout=_CHECKOUT_TIMEOUT,
                kwargs=_POOL_KWARGS,
            )
        return _QUERY_POOL


def warm_pg() -> None:
    """Open one query-pool connection so the first real cue does not wait on TLS."""

    try:
        import psycopg
    except ImportError:
        return
    conn = connect_pg()
    conn.close()


def connect_pg(*, direct: bool = False, url: str | None = None):
    if url is not None:
        try:
            import psycopg
        except ImportError:
            import psycopg2

            return psycopg2.connect(url)
        return psycopg.connect(url, **_POOL_KWARGS)

    try:
        import psycopg
    except ImportError:
        import psycopg2

        return psycopg2.connect(_dsn(direct))

    raw = _pool(direct).getconn()
    return _PooledConn(raw, _pool(direct))


def use_tenant(conn, tenant: str | None) -> None:
    """Bind this open transaction to ``weave_app`` and one tenant.

    A missing tenant leaves the owner in place. Both changes are local to
    the transaction that is already open, or that this call starts.
    """

    if not tenant:
        return
    with conn.cursor() as cur:
        cur.execute(ASSUME_APP_ROLE)
        cur.execute(SET_TENANT, (tenant,))
        cur.fetchone()
