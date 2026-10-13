"""FalkorDB connection from the environment."""

import os


def connect_graph(
    host: str | None = None,
    port: int | None = None,
    graph_name: str | None = None,
):
    from falkordb import FalkorDB

    resolved_host = host or os.environ.get("FALKORDB_HOST", "localhost")
    resolved_port = port if port is not None else int(os.environ.get("FALKORDB_PORT", "6379"))
    resolved_name = graph_name or os.environ.get("FALKORDB_GRAPH", "weave")
    return FalkorDB(host=resolved_host, port=resolved_port).select_graph(resolved_name)
