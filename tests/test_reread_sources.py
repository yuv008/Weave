"""The re-read resets checkpoints and versions for the four polled systems only, and a dry run writes nothing."""

from scripts.reread_sources import SYSTEMS, reread


class _Result:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    def __init__(self):
        self.queries = []

    def query(self, query, params=None):
        self.queries.append((query, params or {}))
        return _Result([[7]])


class FakeCursor:
    def __init__(self):
        self.sql = []
        self.rowcount = 3

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (3,)


def test_dry_run_counts_only():
    graph, cur = FakeGraph(), FakeCursor()
    assert reread("north", graph, cur, dry_run=True) == {"items": 7, "checkpoints": 3}
    assert not any("SET" in query for query, _ in graph.queries)
    assert not any(sql.startswith("DELETE") for sql, _ in cur.sql)


def test_reset_blanks_versions_and_drops_checkpoints_for_the_polled_systems():
    graph, cur = FakeGraph(), FakeCursor()
    assert reread("north", graph, cur, dry_run=False) == {"items": 7, "checkpoints": 3}
    assert cur.sql[0][0].startswith("DELETE FROM ingest_record") and cur.sql[0][1] == ("north", SYSTEMS)
    query, params = graph.queries[0]
    assert "SET i.version = ''" in query and params == {"tenant": "north", "systems": SYSTEMS}
    assert "gmail" not in SYSTEMS
