"""Pruning a source removes its data and the people only it linked to, and a dry run writes nothing."""

import pytest

from scripts.prune_source import prune


class _Result:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    def __init__(self):
        self.queries = []

    def query(self, query, params=None):
        self.queries.append((query, params or {}))
        if "RETURN n.id" in query:
            return _Result([["real:gmail:m1"], ["real:gmail:m2"], ["real:gmail:source"]])
        if "collect(x.id)" in query:
            return _Result([
                ["real:person:pen-pal@x.test", ["real:gmail:m1", "real:gmail:m2"]],
                ["real:person:colleague@x.test", ["real:gmail:m1", "real:slack:msg"]],
                ["real:person:alone@x.test", []],
            ])
        return _Result([])


class FakeCursor:
    def __init__(self):
        self.sql = []

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))

    def fetchone(self):
        return (4,)


def test_dry_run_counts_and_writes_nothing():
    graph, cur = FakeGraph(), FakeCursor()
    report = prune("real", graph, cur, group="gmail:me@x.test", dry_run=True)

    assert report == {"nodes": 3, "orphans": 1, "chunks": 4, "sync_records": 4, "memberships": 4}
    assert not any("DELETE" in q for q, _ in graph.queries)
    assert not any(sql.startswith(("DELETE", "INSERT")) for sql, _ in cur.sql)


def test_pruning_deletes_postgres_rows_first_then_the_nodes_and_only_true_orphans():
    graph, cur = FakeGraph(), FakeCursor()
    prune("real", graph, cur, group="gmail:me@x.test", dry_run=False)

    deleted_tables = [sql.split()[2] for sql, _ in cur.sql if sql.startswith("DELETE")]
    assert deleted_tables == ["chunk", "ingest_record", "group_membership", "source_sync", "connected_source"]
    assert any(sql.startswith("INSERT INTO tenant_acl") for sql, _ in cur.sql)
    final = graph.queries[-1]
    assert "DETACH DELETE" in final[0]
    assert set(final[1]["ids"]) == {"real:gmail:m1", "real:gmail:m2", "real:gmail:source", "real:person:pen-pal@x.test"}


def test_bot_mode_touches_no_source_tables():
    graph, cur = FakeGraph(), FakeCursor()
    prune("pict", graph, cur, bots=True, dry_run=False)

    assert [sql.split()[2] for sql, _ in cur.sql if sql.startswith("DELETE")] == ["chunk"]
    with pytest.raises(ValueError):
        prune("pict", graph, cur, dry_run=True)
