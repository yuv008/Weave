import os
import uuid

import pytest


@pytest.fixture
def graph():
    falkordb = pytest.importorskip("falkordb")
    port = int(os.environ.get("FALKORDB_PORT", "6380"))
    host = os.environ.get("FALKORDB_HOST", "localhost")
    try:
        client = falkordb.FalkorDB(host=host, port=port)
        name = f"weave_test_{uuid.uuid4().hex}"
        database = client.select_graph(name)
        database.query("RETURN 1")
    except Exception as exc:
        pytest.skip(f"FalkorDB is not available: {exc}")
    from app.graph.upsert import ensure_indexes

    ensure_indexes(database)
    yield database
    database.delete()
