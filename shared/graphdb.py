"""Open ``co:{tenant}`` or ``u:{tenant}:{user}``.

``FALKORDB_URL`` wins when set (hosted FalkorDB, for example
``falkors://user:password@host:port``). Otherwise ``FALKORDB_HOST`` and
``FALKORDB_PORT`` are used, as in ``app/graph/client.py``.

Agents read through ``graph.ro_query``; only ingestion and the fixture seed
call ``graph.query``.
"""

import os

from shared.schema import company_graph, private_graph


def connect(host: str | None = None, port: int | None = None):
    from falkordb import FalkorDB

    url = os.environ.get("FALKORDB_URL", "").strip()
    if url and host is None and port is None:
        return FalkorDB.from_url(url)
    resolved_host = host or os.environ.get("FALKORDB_HOST", "localhost")
    resolved_port = port if port is not None else int(os.environ.get("FALKORDB_PORT", "6379"))
    return FalkorDB(host=resolved_host, port=resolved_port)


def open_graph(tenant: str, user: str | None = None, *, host: str | None = None, port: int | None = None):
    name = private_graph(tenant, user) if user else company_graph(tenant)
    return connect(host, port).select_graph(name)
