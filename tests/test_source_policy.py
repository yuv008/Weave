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


class _Recorder:
    """Records each statement. Owner emails come back for the owner query."""

    def __init__(self, owners=("ada@north.test",)):
        self.owners, self.sql = list(owners), []

    def __call__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def cursor(self):
        return self

    def execute(self, sql, params=()):
        self.sql.append((" ".join(sql.split()), tuple(params)))
        self.last = sql

    def fetchone(self):
        return None

    def fetchall(self):
        return [(email,) for email in self.owners] if "tenant_membership" in self.last else []

    def commit(self):
        pass


def _ensure(monkeypatch, recorder, **kwargs):
    from app.graph import policy

    monkeypatch.setattr(policy, "_connected", {})
    monkeypatch.setattr("shared.pg.connect_pg", recorder)
    policy.ensure_connected_source("north", "github", "north-org/secret", **kwargs)
    return policy


def test_a_private_repo_is_kept_to_the_owners(monkeypatch):
    recorder = _Recorder()
    policy = _ensure(monkeypatch, recorder, org_wide=False)

    found = allowed_source("github", "north-org/secret", "north")
    assert found is not None and found[1].org_wide is False
    memberships = [params for sql, params in recorder.sql if sql.startswith("INSERT INTO group_membership")]
    assert memberships == [("north", "north:person:ada@north.test", "github:north-org/secret", "github")]
    assert any(sql.startswith("INSERT INTO tenant_acl") for sql, _ in recorder.sql)
    assert policy._connected[("north", "github:north-org/secret")].org_wide is False


def test_a_public_repo_stays_company_wide_without_memberships(monkeypatch):
    recorder = _Recorder()
    _ensure(monkeypatch, recorder)

    assert allowed_source("github", "north-org/secret", "north")[1].org_wide is True
    assert not any("group_membership" in sql for sql, _ in recorder.sql)


def test_an_org_wide_row_narrows_but_never_widens(monkeypatch):
    recorder = _Recorder()
    policy = _ensure(monkeypatch, recorder)
    key = ("north", "github:north-org/secret")
    assert policy._connected[key].org_wide is True

    policy.ensure_connected_source("north", "github", "north-org/secret", org_wide=False)
    assert policy._connected[key].org_wide is False
    narrowed = [sql for sql, _ in recorder.sql if "DO UPDATE SET org_wide" in sql]
    assert len(narrowed) == 2

    writes = len(recorder.sql)
    policy.ensure_connected_source("north", "github", "north-org/secret", org_wide=True)
    assert policy._connected[key].org_wide is False
    assert len(recorder.sql) == writes


def test_the_gate_shows_a_private_repo_item_to_the_owner_only():
    from agents.gate.rules import item_is_visible, person_groups
    from shared.config import SourcePolicy, source_acl
    from shared.schema import Prop

    group = "github:north-org/secret"
    narrowed = SourcePolicy(name="secret", min_level=0, cascade_policy="manual", authority=2, org_wide=False)
    item = {Prop.MIN_LEVEL: 0, Prop.ACL_GROUPS: source_acl(group, narrowed)}
    memberships = {"north:person:ada@north.test": {group}}
    owner = person_groups("north:person:ada@north.test", 0, memberships)
    member = person_groups("north:person:bo@north.test", 0, memberships)

    assert item_is_visible(item, 0, owner, {group})
    assert not item_is_visible(item, 0, member, {group})


def test_a_repo_whose_privacy_cannot_be_read_counts_as_private():
    from scripts.run_worker import _repo_is_private

    class Client:
        def __init__(self, payload=None, fail=False):
            self.payload, self.fail = payload, fail

        def get_repo(self, owner, repo):
            if self.fail:
                raise RuntimeError("404")
            return type("Result", (), {"payload": self.payload})()

    assert _repo_is_private(Client(fail=True), "o", "r") is True
    assert _repo_is_private(Client({}), "o", "r") is True
    assert _repo_is_private(Client({"private": True}), "o", "r") is True
    assert _repo_is_private(Client({"private": False}), "o", "r") is False
