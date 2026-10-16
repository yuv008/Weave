import uuid

import pytest


@pytest.fixture
def graph():
    pytest.importorskip("falkordb")
    from shared.env import load_env
    from shared.graphdb import connect

    load_env()
    try:
        client = connect()
        name = f"weave_test_{uuid.uuid4().hex}"
        database = client.select_graph(name)
        database.query("RETURN 1")
    except Exception as exc:
        pytest.skip(f"FalkorDB is not available: {type(exc).__name__}")
    from app.graph.upsert import ensure_indexes

    ensure_indexes(database)
    yield database
    database.delete()
