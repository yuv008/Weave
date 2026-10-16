from app.ingest.chunks import index_source
from app.ingest.records import MemoryStore


class _Graph:
    def __init__(self, rows):
        self.rows = rows

    def query(self, query, params=None):
        return type("Result", (), {"result_set": self.rows})()


def test_index_replaces_a_changed_excerpt_and_drops_a_deleted_item():
    graph = _Graph(
        [
            ("item-a", "tenant", "hello", False, ["slack:C1"], 0),
            ("item-b", "tenant", "gone", False, ["slack:C1"], 0),
        ]
    )
    store = MemoryStore()

    def embed(texts):
        return [[0.1, 0.2] for _ in texts]

    assert index_source(graph, store, "source", embedder=embed) == 2
    graph.rows[0] = ("item-a", "tenant", "hello again", False, ["slack:C1"], 0)
    graph.rows[1] = ("item-b", "tenant", "gone", True, ["slack:C1"], 0)
    assert index_source(graph, store, "source", embedder=embed) == 1
    assert store._chunks["item-a:chunk:0"]["text"] == "hello again"
    assert "item-b:chunk:0" not in store._chunks


def test_memory_store_skips_the_model():
    graph = _Graph([("item-a", "tenant", "hello", False, ["slack:C1"], 0)])
    assert index_source(graph, MemoryStore(), "source") == 0
