"""Postgres connections. Uses psycopg 3, or psycopg2 if that is what is installed.

On Neon, ``DATABASE_URL`` is the pooled endpoint (PgBouncer, transaction
mode) for normal queries. ``DATABASE_URL_UNPOOLED`` is the direct endpoint;
migrations must use it. Locally both can be the same URL.

``connect_pg()`` stays the table owner. Migrations, seeds, and tests use that
and still see every row, because row level security is not forced on the
owner. Isolated app queries call ``use_tenant`` in the same transaction as
the statement. ``SET LOCAL`` and ``set_config(..., true)`` end at commit or
rollback, so a transaction-mode pooler cannot leak the role or the tenant.
"""

import os

APP_ROLE = "weave_app"
ASSUME_APP_ROLE = "SET LOCAL ROLE weave_app"
SET_TENANT = "SELECT set_config('weave.tenant', %s, true)"


def connect_pg(*, direct: bool = False, url: str | None = None):
    dsn = url
    if dsn is None and direct:
        dsn = os.environ.get("DATABASE_URL_UNPOOLED")
    if dsn is None:
        dsn = os.environ["DATABASE_URL"]
    try:
        import psycopg
    except ImportError:
        import psycopg2

        return psycopg2.connect(dsn)
    return psycopg.connect(dsn)


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
