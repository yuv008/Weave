"""Listed sources stay allowed. A container nobody connected is not written."""

from app.graph.policy import allowed_source


def test_listed_notion_workspace_is_allowed():
    found = allowed_source("notion", "sales-deals")
    assert found is not None
    group, policy = found
    assert group == "notion:sales-deals"
    assert policy.min_level == 0
    assert policy.authority == 3


def test_unknown_container_is_closed():
    assert allowed_source("notion", "not-listed-workspace-xyz") is None


class _Rows:
    """connected_source as a dict keyed by (tenant, group). ``fail`` raises like a dropped connection."""

    def __init__(self, rows, fail=False):
        self.rows, self.fail, self.reads = rows, fail, 0

    def __call__(self):
        if self.fail:
            raise ConnectionError("postgres is down")
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.reads += 1
        self.found = self.rows.get(tuple(params))

    def fetchone(self):
        return self.found

    def commit(self):
        pass


def _policy_with(monkeypatch, rows):
    from app.graph import policy

    monkeypatch.setattr(policy, "_connected", {})
    monkeypatch.setattr("shared.pg.connect_pg", rows)
    return policy


def test_a_connected_source_is_allowed_only_for_its_company(monkeypatch):
    row = ("north repo", 0, "manual", 2, True)
    rows = _Rows({("north", "github:north-org/app"): row})
    _policy_with(monkeypatch, rows)

    assert allowed_source("github", "north-org/app", "north") is not None
    assert allowed_source("github", "north-org/app", "south") is None
    # No company means no connected row matches.
    assert allowed_source("github", "north-org/app") is None


def test_a_failed_lookup_is_not_remembered(monkeypatch):
    rows = _Rows({("north", "slack:C9"): ("C9", 0, "manual", 1, True)}, fail=True)
    _policy_with(monkeypatch, rows)

    assert allowed_source("slack", "C9", "north") is None
    rows.fail = False
    assert allowed_source("slack", "C9", "north") is not None


def test_a_row_written_by_another_process_is_seen(monkeypatch):
    rows = _Rows({})
    _policy_with(monkeypatch, rows)

    assert allowed_source("slack", "C7", "north") is None
    rows.rows[("north", "slack:C7")] = ("C7", 0, "manual", 1, True)
    assert allowed_source("slack", "C7", "north") is not None
    reads = rows.reads
    assert allowed_source("slack", "C7", "north") is not None
    assert rows.reads == reads
