"""The access-tag backfill opens org-wide sources, narrows the rest to owners, and leaves unknown groups alone."""

from app.graph import policy
from scripts.apply_source_acl import reconcile_tenant


class _Result:
    def __init__(self, rows):
        self.result_set = rows


class FakeGraph:
    def __init__(self):
        self.queries = []

    def query(self, query, params=None):
        self.queries.append((query, params or {}))
        if "RETURN DISTINCT g" in query:
            return _Result([["org:all"], ["slack:C0ENGPUB"], ["slack:C0ENGSSO"], ["notion:gone"]])
        return _Result([[3]])


class FakeCursor:
    def __init__(self):
        self.sql = []
        self.rowcount = 2

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), params))

    def fetchall(self):
        return [("ada@north.test",)]

    def fetchone(self):
        return (0,)


def _run(monkeypatch, dry_run):
    monkeypatch.setattr(policy, "_connected_policy", lambda tenant, group: None)
    graph, cur = FakeGraph(), FakeCursor()
    report = {row["group"]: row for row in reconcile_tenant("north", graph, cur, dry_run=dry_run)}
    return report, graph, cur


def test_dry_run_reports_what_would_change_and_writes_nothing(monkeypatch):
    report, graph, cur = _run(monkeypatch, dry_run=True)

    assert report["slack:C0ENGPUB"]["action"] == "open"
    assert report["slack:C0ENGSSO"]["action"] == "owners_only"
    assert report["slack:C0ENGSSO"]["owner_memberships"] == 1
    assert report["notion:gone"] == {"group": "notion:gone", "action": "no_policy"}
    assert not any(" SET " in query for query, _ in graph.queries)
    assert not any(sql.startswith(("UPDATE", "INSERT")) for sql, _ in cur.sql)


def test_applying_opens_narrows_adds_the_owner_and_bumps_the_epoch(monkeypatch):
    report, graph, cur = _run(monkeypatch, dry_run=False)

    opened = [p for q, p in graph.queries if "+ $org" in q]
    narrowed = [p for q, p in graph.queries if "g <> $org" in q]
    assert [p["group"] for p in opened] == ["slack:C0ENGPUB"]
    assert [p["group"] for p in narrowed] == ["slack:C0ENGSSO"]
    assert any("array_append" in sql and params[2] == "slack:C0ENGPUB" for sql, params in cur.sql)
    assert any("array_remove" in sql and params[2] == "slack:C0ENGSSO" for sql, params in cur.sql)
    member = [params for sql, params in cur.sql if sql.startswith("INSERT INTO group_membership")]
    assert member == [("north", "north:person:ada@north.test", "slack:C0ENGSSO", "slack")]
    assert any(sql.startswith("INSERT INTO tenant_acl") for sql, _ in cur.sql)
    assert not any("notion:gone" in str(p) for _, p in graph.queries[1:])
