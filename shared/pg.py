"""Postgres connections. Uses psycopg 3, or psycopg2 if that is what is installed.

On Neon, ``DATABASE_URL`` is the pooled endpoint (PgBouncer, transaction
mode) for normal queries. ``DATABASE_URL_UNPOOLED`` is the direct endpoint;
migrations must use it. Locally both can be the same URL.
"""

import os


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
