"""FalkorDB connection for ingestion.

New writes use ``co:{WEAVE_TENANT}``. The fixture graph ``co:acme`` is refused.
Tests pass a graph object and do not call this helper.
"""

import os

from shared.graphdb import connect
from shared.schema import company_graph


def connect_graph(
    host: str | None = None,
    port: int | None = None,
    graph_name: str | None = None,
):
    tenant = os.environ.get("WEAVE_TENANT", "").strip()
    if not tenant or tenant == "acme":
        raise ValueError("WEAVE_TENANT must be set and must not be acme")
    name = graph_name or company_graph(tenant)
    return connect(host, port).select_graph(name)
