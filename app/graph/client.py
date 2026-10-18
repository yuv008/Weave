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
    tenant: str | None = None,
):
    chosen = (tenant or os.environ.get("WEAVE_TENANT", "")).strip()
    if not chosen or chosen == "acme":
        raise ValueError("WEAVE_TENANT must be set and must not be acme")
    name = graph_name or company_graph(chosen)
    return connect(host, port).select_graph(name)
