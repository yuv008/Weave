"""Row level security: migration shape, transaction-local tenant switch, and a live isolation check.

The live check needs DATABASE_URL and migrations/007_rls.sql already applied.
It skips when Postgres is missing, the same way the ingest contract test does.
Owner connections are not the isolation proof: tenant B reads as weave_app.
"""

import os
import re
import uuid
from pathlib import Path

import pytest

from agents.gate.principal import Kind, Principal, Surface
from agents.gate.stores import TOKEN_OWNER_SQL, PostgresGateStore
from agents.mcp.audit import PostgresAudit
from agents.memory.store import ConversationNotFound, Episode, PostgresMemoryStore, Task
from agents.slack.claims import PostgresReplyClaims
from agents.watcher.outbox import PostgresOutbox
from app.ingest.records import PostgresStore
from shared.pg import APP_ROLE, ASSUME_APP_ROLE, SET_TENANT, connect_pg, use_tenant

ROOT = Path(__file__).resolve().parents[1]
MIGRATION = ROOT / "migrations" / "007_rls.sql"


#: Tables only the owner connection reads. They have no tenant policy, so weave_app must not hold them.
OWNER_ONLY = {"connection_secret", "app_user", "tenant_membership", "app_session", "login_attempt", "connected_source", "model_usage", "email_token", "invite"}


def _migration() -> str:
    return MIGRATION.read_text(encoding="utf-8")


def test_owner_only_tables_end_up_without_a_weave_app_grant():
    statements = []
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        text = path.read_text(encoding="utf-8")
        statements += re.findall(r"\b(GRANT|REVOKE)\b[^;]*?\bON\s+(?:TABLE\s+)?(\w+)\s+(?:TO|FROM)\s+weave_app", text)
    for name in OWNER_ONLY:
        last = [verb for verb, table in statements if table == name][-1:]
        assert last != ["GRANT"], name


class RecCursor:
    def __init__(self, conn: "RecConn"):
        self.conn = conn
        self.last = ""

    def execute(self, sql, params=None):
        self.last = sql
        self.conn.log.append((sql, params))

    def fetchone(self):
        if "set_config" in self.last:
            return ("set",)
        if "FOR UPDATE SKIP LOCKED" in self.last:
            return ("evt", "tenant-a", "fact", "superseded", None, None)
        return None

    def fetchall(self):
        return []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


class RecConn:
    def __init__(self):
        self.log: list[tuple] = []

    def cursor(self):
        return RecCursor(self)

    def commit(self):
        self.log.append(("COMMIT", None))

    def rollback(self):
        self.log.append(("ROLLBACK", None))

    def close(self):
        self.log.append(("CLOSE", None))


def _sqls(conn: RecConn) -> list[str]:
    return [sql for sql, _ in conn.log]


def test_migration_is_repeatable_and_does_not_force_the_owner():
    sql = _migration()
    assert "IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'weave_app')" in sql
    assert "CREATE ROLE weave_app NOLOGIN" in sql
    assert "BYPASSRLS" not in sql
    assert "FORCE ROW LEVEL SECURITY" not in sql
    assert "ADD COLUMN IF NOT EXISTS tenant_id" in sql
    assert "DROP POLICY IF EXISTS weave_tenant" in sql
    assert "CREATE OR REPLACE FUNCTION weave_token_owner" in sql
    assert "SECURITY DEFINER" in sql
    assert "SET search_path = public" in sql
    assert "revoked_at IS NULL" in sql
    assert "LANGUAGE plpgsql" in sql
    assert "GRANT weave_app TO CURRENT_USER" in sql


def test_policies_are_fail_closed_and_turns_do_not_subquery():
    sql = _migration()
    earlier = []
    for path in sorted((ROOT / "migrations").glob("*.sql")):
        if path.name == "007_rls.sql":
            continue
        earlier.append(path.read_text(encoding="utf-8"))
    tables = re.findall(r"CREATE TABLE IF NOT EXISTS (\w+)", "\n".join(earlier))
    assert tables
    for name in tables:
        assert f"'{name}'" in sql or name == "playbooks" or name in OWNER_ONLY

    policy = sql.split("$policy$", 2)[1]
    assert policy == (
        "CREATE POLICY weave_tenant ON %I FOR ALL TO weave_app "
        "USING (tenant_id = current_setting('weave.tenant', true)) "
        "WITH CHECK (tenant_id = current_setting('weave.tenant', true))"
    )
    assert "SELECT" not in policy
    assert "conversations" not in policy
    assert "conversation_turns" in sql
    assert "FROM conversations" in sql
    playbooks = sql.split("CREATE POLICY weave_tenant ON playbooks", 1)[1]
    assert "tenant_id = current_setting('weave.tenant', true) OR tenant_id IS NULL" in playbooks


def test_token_lookup_uses_the_definer_function():
    assert TOKEN_OWNER_SQL == "SELECT tenant_id, person_id FROM weave_token_owner(%s)"
    assert "api_token" not in TOKEN_OWNER_SQL


def test_use_tenant_sets_a_transaction_local_role_and_setting():
    log: list[tuple] = []

    class Cur:
        def execute(self, sql, params=None):
            log.append((sql, params))

        def fetchone(self):
            return ("tenant-a",)

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

    class Conn:
        def cursor(self):
            return Cur()

    use_tenant(Conn(), "tenant-a")
    assert log == [
        (ASSUME_APP_ROLE, None),
        (SET_TENANT, ("tenant-a",)),
    ]
    assert ASSUME_APP_ROLE == "SET LOCAL ROLE weave_app"
    assert SET_TENANT.endswith(", true)")
    assert APP_ROLE == "weave_app"


def test_use_tenant_without_a_tenant_leaves_the_owner():
    class Boom:
        def cursor(self):
            raise AssertionError("owner connection must not switch role")

    use_tenant(Boom(), None)
    use_tenant(Boom(), "")
    assert "tenant" not in connect_pg.__code__.co_varnames


def test_postgres_memory_reuses_one_open_connection():
    class Live(RecConn):
        def __init__(self):
            super().__init__()
            self.closed = False

        def close(self):
            self.closed = True
            super().close()

    opened = []

    def connect():
        conn = Live()
        opened.append(conn)
        return conn

    store = PostgresMemoryStore(connect)
    assert store.get_episode("ep1") is None
    assert store.get_episode("ep2") is None
    assert len(opened) == 1
    assert opened[0].closed is False
    assert _sqls(opened[0]).count("CLOSE") == 0


def test_stores_pass_the_tenant_they_already_have():
    memory = RecConn()
    store = PostgresMemoryStore(lambda: memory)
    store.add_episode(Episode("tenant-a", "p1", "answer", question="SSO"))
    assert memory.log[0] == (ASSUME_APP_ROLE, None)
    assert memory.log[1] == (SET_TENANT, ("tenant-a",))
    assert "INSERT INTO episodes" in memory.log[2][0]

    plain = RecConn()
    PostgresMemoryStore(lambda: plain).get_episode("ep1")
    assert ASSUME_APP_ROLE not in _sqls(plain)

    turns = RecConn()
    PostgresMemoryStore(lambda: turns).add_turn("conv", "user", "q", [])
    assert ASSUME_APP_ROLE not in _sqls(turns)
    assert "tenant_id" in turns.log[0][0]
    assert "SELECT tenant_id FROM conversations WHERE id = %s" in turns.log[0][0]

    shared = RecConn()
    with pytest.raises(TypeError):
        PostgresMemoryStore(lambda: shared).put_playbook(None, "sales", "draft", ["decision"], None)
    assert ASSUME_APP_ROLE not in _sqls(shared)

    blank = RecConn()
    assert PostgresMemoryStore(lambda: blank).playbook("", "sales", "draft") is None
    assert ASSUME_APP_ROLE not in _sqls(blank)

    scoped = RecConn()
    with pytest.raises(ConversationNotFound):
        PostgresMemoryStore(lambda: scoped).create_conversation("tenant-a", "p1", "web", "SSO")
    assert scoped.log[0] == (ASSUME_APP_ROLE, None)
    assert scoped.log[1][1] == ("tenant-a",)

    tasks = RecConn()
    assert PostgresMemoryStore(lambda: tasks).add_task(Task("tenant-a", "slack_dm", "ready")) is None
    assert tasks.log[1][1] == ("tenant-a",)

    gate = RecConn()
    snap = PostgresGateStore(lambda: gate).snapshot("tenant-a")
    assert snap.acl_epoch == 0
    assert gate.log[0] == (ASSUME_APP_ROLE, None)
    assert gate.log[1][1] == ("tenant-a",)
    assert "group_membership" in gate.log[2][0]

    tokens = RecConn()
    assert PostgresGateStore(lambda: tokens).token_owner("hash") is None
    assert tokens.log[0][0] == TOKEN_OWNER_SQL
    assert ASSUME_APP_ROLE not in _sqls(tokens)

    principal = Principal("tenant-a", Kind.PERSON, Surface.WEB, 0, frozenset(), 1, user_id="p1")
    audit = RecConn()
    PostgresAudit(lambda: audit).record(
        request_id="r", principal=principal, tool="remember", args={}, node_ids=[], hidden=0, outcome="ok", ms=1,
    )
    assert audit.log[0] == (ASSUME_APP_ROLE, None)
    assert audit.log[1][1] == ("tenant-a",)
    assert "INSERT INTO audit_log" in audit.log[2][0]
    assert audit.log[3][0] == "COMMIT"

    outbox_conn = RecConn()
    with PostgresOutbox(lambda: outbox_conn).claim("tenant-a") as event:
        assert event is not None and event.tenant == "tenant-a"
        assert "COMMIT" not in _sqls(outbox_conn)
    outbox_sql = _sqls(outbox_conn)
    assert outbox_sql[0] == ASSUME_APP_ROLE
    assert outbox_sql.count(ASSUME_APP_ROLE) == 1
    assert outbox_sql.index("UPDATE change_event SET processed = true WHERE id = %s") < outbox_sql.index("COMMIT")

    claims = RecConn()
    assert PostgresReplyClaims(lambda: claims).claim("Ev1", "tenant-a", "C1", "1.0") is False
    assert claims.log[0] == (ASSUME_APP_ROLE, None)
    assert claims.log[1][1] == ("tenant-a",)
    assert "INSERT INTO slack_reply" in claims.log[2][0]

    made: list[RecConn] = []

    def connect(**kwargs):
        conn = RecConn()
        made.append(conn)
        return conn

    monkeypatch = pytest.MonkeyPatch()
    try:
        monkeypatch.setattr("shared.pg.connect_pg", connect)
        records = PostgresStore("postgres://local/weave")
        assert records.merge("rec", "tenant-a", "sync_state", {"status": "ok"}) is None
        assert records.create_once("rec2", "tenant-b", "slack_event", {}) is False
        records.delete("rec")
        assert records.list_kind("sync_state") == []
        assert records.get("rec") is None
    finally:
        monkeypatch.undo()

    assert made[0].log[0] == (ASSUME_APP_ROLE, None)
    assert made[0].log[1][1] == ("tenant-a",)
    assert ASSUME_APP_ROLE not in _sqls(made[1])
    assert made[2].log[1][1] == ("tenant-b",)
    assert ASSUME_APP_ROLE not in _sqls(made[3])
    assert ASSUME_APP_ROLE not in _sqls(made[4])
    assert ASSUME_APP_ROLE not in _sqls(made[5])


def test_weave_app_cannot_read_another_tenants_row():
    """Insert as tenant A and read as tenant B, both through weave_app."""

    dsn = os.environ.get("DATABASE_URL", "")
    if not dsn:
        pytest.skip("DATABASE_URL is not set")
    try:
        probe = connect_pg(url=dsn)
    except Exception:
        pytest.skip("Postgres is not available")
    try:
        with probe.cursor() as cur:
            cur.execute("SELECT rolbypassrls, rolcanlogin FROM pg_roles WHERE rolname = 'weave_app'")
            role = cur.fetchone()
            cur.execute(
                "SELECT 1 FROM pg_policies WHERE schemaname = 'public'"
                " AND tablename = 'group_membership' AND policyname = 'weave_tenant'"
            )
            policy = cur.fetchone()
        probe.rollback()
    finally:
        probe.close()
    if role is None or policy is None:
        pytest.skip("migrations/007_rls.sql is not applied")
    assert role[0] is False and role[1] is False

    tenant_a = f"rls_{uuid.uuid4().hex}"
    tenant_b = f"rls_{uuid.uuid4().hex}"
    person = f"person_{uuid.uuid4().hex}"
    inserted = False
    try:
        conn = connect_pg(url=dsn)
        try:
            use_tenant(conn, tenant_a)
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == APP_ROLE
                cur.execute(
                    "INSERT INTO group_membership (tenant_id, person_id, acl_group, source_system)"
                    " VALUES (%s, %s, %s, 'rls')",
                    (tenant_a, person, "slack:rls"),
                )
            conn.commit()
            inserted = True
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] != APP_ROLE
        finally:
            conn.close()

        conn = connect_pg(url=dsn)
        try:
            use_tenant(conn, tenant_b)
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == APP_ROLE
                cur.execute(
                    "SELECT tenant_id FROM group_membership WHERE person_id = %s",
                    (person,),
                )
                assert cur.fetchall() == []
            conn.rollback()
        finally:
            conn.close()

        conn = connect_pg(url=dsn)
        try:
            use_tenant(conn, tenant_a)
            with conn.cursor() as cur:
                cur.execute("SELECT current_user")
                assert cur.fetchone()[0] == APP_ROLE
                cur.execute(
                    "SELECT tenant_id FROM group_membership WHERE person_id = %s",
                    (person,),
                )
                assert cur.fetchall() == [(tenant_a,)]
            conn.rollback()
        finally:
            conn.close()
    finally:
        if inserted:
            owner = connect_pg(url=dsn)
            try:
                with owner.cursor() as cur:
                    cur.execute("DELETE FROM group_membership WHERE person_id = %s", (person,))
                owner.commit()
            finally:
                owner.close()
