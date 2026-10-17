"""GET /ops offline: OPS_TOKEN auth, WEAVE_TENANT only, numbers from a fake connection."""

from decimal import Decimal

import pytest
from starlette.testclient import TestClient

from agents.gate.resolve import NotAuthorized
from agents.mcp.server import build_app
from agents.runtime import Runtime
from app.ingest.records import SYNC_FAILURE
from shared.config import load_config

TOKEN = "ops-test-token"
TENANT = "tenant-ops"
OTHER = "other-tenant"
AUTH = {"authorization": f"Bearer {TOKEN}"}

_ROWS = {
    "source_sync": [("src-eng", "org:eng", Decimal("90")), ("src-sales", "org:sales", Decimal("10"))],
    "lag": [("sync_failure", Decimal("400")), ("sync_state", Decimal("15"))],
    "failures": [(2,)],
    "events": [(4,)],
    "tasks": [(3,)],
    "p95": [(42.5,)],
}

_OPEN = ("proposed", "ready", "claimed")
_TERMINAL = ("accepted", "done", "expired", "failed", "rejected", "sent")
_BANNED = ("payload", "quote", "message", "body", "draft")


class _Resolver:
    def for_token(self, token, surface):
        raise NotAuthorized()


def _no_graph(tenant):
    raise AssertionError("ops must not open the graph")


def _no_pg(*args, **kwargs):
    raise AssertionError("ops must use connect_pg, not runtime.pg_connect")


class FakeCursor:
    def __init__(self, conn):
        self.conn = conn
        self._result = []

    def execute(self, sql, params=None):
        compact = " ".join(sql.split())
        head = compact.split(None, 1)[0].lower()
        if head in {"insert", "update", "delete", "alter", "drop", "create", "truncate"}:
            raise AssertionError(head)
        self.conn.statements.append((compact, params))
        self._result = _match(compact, self.conn.rows)

    def fetchall(self):
        return list(self._result)

    def fetchone(self):
        if not self._result:
            return None
        return self._result[0]

    def close(self):
        self.conn.closed_cursor = True


class FakeConnection:
    def __init__(self, rows):
        self.rows = rows
        self.statements = []
        self.closed = False
        self.rolled_back = False
        self.closed_cursor = False

    def cursor(self):
        return FakeCursor(self)

    def commit(self):
        raise AssertionError("read-only ops tried to commit")

    def rollback(self):
        self.rolled_back = True

    def close(self):
        self.closed = True


def _match(sql, rows):
    compact = sql.lower()
    if compact.startswith("set"):
        return []
    if "from source_sync" in compact:
        return rows["source_sync"]
    if "from ingest_record" in compact and "group by" in compact:
        return rows["lag"]
    if "from ingest_record" in compact:
        return rows["failures"]
    if "from change_event" in compact:
        return rows["events"]
    if "from tasks" in compact:
        return rows["tasks"]
    if "from audit_log" in compact:
        return rows["p95"]
    raise AssertionError(sql)


def _app(monkeypatch, rows, *, token=TOKEN, tenant=TENANT):
    if token is None:
        monkeypatch.delenv("OPS_TOKEN", raising=False)
    else:
        monkeypatch.setenv("OPS_TOKEN", token)
    if tenant is None:
        monkeypatch.delenv("WEAVE_TENANT", raising=False)
    else:
        monkeypatch.setenv("WEAVE_TENANT", tenant)
    conns = []

    def connect(*args, **kwargs):
        conn = FakeConnection(rows)
        conns.append(conn)
        return conn

    monkeypatch.setattr("agents.mcp.ops.connect_pg", connect)
    runtime = Runtime(_no_graph, load_config(), resolver=_Resolver(), pg_connect=_no_pg)
    return build_app(runtime), conns


def _expected(p95=42.5):
    return {
        "permission_sync": [
            {"source_id": "src-eng", "acl_group": "org:eng", "age_seconds": 90},
            {"source_id": "src-sales", "acl_group": "org:sales", "age_seconds": 10},
        ],
        "ingestion": {
            "lag_seconds": [
                {"kind": "sync_failure", "age_seconds": 400},
                {"kind": "sync_state", "age_seconds": 15},
            ],
            "sync_failures": 2,
        },
        "queue": {"change_events_unprocessed": 4, "tasks_open": 3},
        "p95_ms": p95,
    }


def test_sync_failure_kind_is_the_ingest_constant():
    assert SYNC_FAILURE == "sync_failure"


@pytest.mark.parametrize("token", [None, "   "])
def test_ops_is_hidden_when_the_token_is_unset(monkeypatch, token):
    app, conns = _app(monkeypatch, _ROWS, token=token)
    with TestClient(app) as client:
        responses = [
            client.get("/ops"),
            client.get("/ops", headers={"authorization": "Bearer nope"}),
            client.get("/ops?format=html"),
            client.post("/ops", headers=AUTH),
        ]
    for response in responses:
        assert response.status_code == 404
        assert response.text == "not found"
        assert TOKEN not in response.text
    assert conns == []


def test_ops_rejects_a_missing_or_wrong_token(monkeypatch):
    app, conns = _app(monkeypatch, _ROWS)
    with TestClient(app) as client:
        missing = client.get("/ops")
        wrong = client.get("/ops", headers={"authorization": "Bearer nope"})
        basic = client.get("/ops", headers={"authorization": "Basic dG9r"})
        blank = client.get("/ops", headers={"authorization": "Bearer"})
        posted = client.post("/ops", headers=AUTH)
    for response in (missing, wrong, basic, blank):
        assert response.status_code == 401
        assert response.text == "not authorized"
        assert response.headers["www-authenticate"] == "Bearer"
        assert TOKEN not in response.text
    assert posted.status_code == 405
    assert conns == []


def test_ops_json_is_tenant_scoped_numbers(monkeypatch):
    app, conns = _app(monkeypatch, _ROWS)
    with TestClient(app) as client:
        response = client.get(f"/ops?tenant={OTHER}&format=json", headers={**AUTH, "accept": "application/json"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/json")
    assert response.json() == _expected()
    assert OTHER not in response.text
    assert TENANT not in response.text
    _assert_queries(conns[0])


def test_ops_html_is_the_same_numbers_without_message_text(monkeypatch):
    app, _conns = _app(monkeypatch, _ROWS)
    with TestClient(app) as client:
        by_header = client.get("/ops", headers={**AUTH, "accept": "text/html, application/xhtml+xml"})
        by_query = client.get(
            f"/ops?format=html&tenant={OTHER}",
            headers={**AUTH, "accept": "application/json"},
        )
    for response in (by_header, by_query):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/html")
        pre = response.text.split("<pre>", 1)[1].split("</pre>", 1)[0]
        assert '"' not in pre and "'" not in pre
        for number in ("90", "10", "400", "15", "2", "4", "3", "42.5"):
            assert number in pre
        for banned in (*_BANNED, OTHER, TENANT, TOKEN):
            assert banned not in pre
        assert "src-eng" in pre and "org:eng" in pre and "sync_state" in pre


def test_ops_p95_is_null_when_the_window_is_empty(monkeypatch):
    rows = dict(_ROWS, p95=[(None,)])
    app, _conns = _app(monkeypatch, rows)
    with TestClient(app) as client:
        body = client.get("/ops", headers=AUTH)
        page = client.get("/ops?format=html", headers=AUTH)
    assert body.json() == _expected(p95=None)
    pre = page.text.split("<pre>", 1)[1].split("</pre>", 1)[0]
    assert pre.rstrip().endswith("p95_ms")
    assert "None" not in pre


def test_ops_hides_database_errors(monkeypatch, caplog):
    monkeypatch.setenv("OPS_TOKEN", TOKEN)
    monkeypatch.setenv("WEAVE_TENANT", TENANT)

    def connect(*args, **kwargs):
        raise RuntimeError("postgres://user:secret-password@db.example/weave")

    monkeypatch.setattr("agents.mcp.ops.connect_pg", connect)
    app = build_app(Runtime(_no_graph, load_config(), resolver=_Resolver(), pg_connect=_no_pg))
    with TestClient(app) as client:
        response = client.get("/ops", headers=AUTH)
    assert response.status_code == 503
    assert response.text == "unavailable"
    assert "secret-password" not in response.text
    assert "secret-password" not in caplog.text


def test_ops_refuses_to_run_without_a_tenant(monkeypatch):
    app, conns = _app(monkeypatch, _ROWS, tenant=None)
    with TestClient(app) as client:
        response = client.get("/ops", headers=AUTH)
    assert response.status_code == 503
    assert response.text == "unavailable"
    assert conns == []


def test_user_routes_still_require_a_user_token(monkeypatch):
    app, conns = _app(monkeypatch, _ROWS)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"}, headers=AUTH)
    assert response.status_code == 401
    assert conns == []


def _assert_queries(conn):
    assert conn.closed and conn.rolled_back and conn.closed_cursor
    statements = conn.statements
    assert [sql.split(None, 1)[0].upper() for sql, _ in statements] == ["SET", *["SELECT"] * 6]
    assert statements[0][0] == "SET TRANSACTION READ ONLY"
    for sql, params in statements[1:]:
        assert params[0] == TENANT
        assert TENANT not in sql and OTHER not in sql
        lowered = sql.lower()
        for banned in _BANNED:
            assert banned not in lowered
    sync, lag, failures, events, tasks, p95 = (sql for sql, _ in statements[1:])
    assert "from source_sync" in sync.lower() and "last_good_sync_at" in sync.lower()
    assert "from ingest_record" in lag.lower() and "max(updated_at)" in lag.lower() and "group by kind" in lag.lower()
    assert statements[3][1] == (TENANT, "sync_failure")
    assert "kind = %s" in failures.lower()
    assert "from change_event" in events.lower() and "not processed" in events.lower()
    assert "from tasks" in tasks.lower() and "not in" in tasks.lower()
    for state in _TERMINAL:
        assert f"'{state}'" in tasks
    for state in _OPEN:
        assert state not in tasks
    assert "percentile_cont(0.95)" in p95.lower()
    assert "interval '15 minutes'" in p95.lower()
    assert "ms::double precision" in p95.lower()
    assert "from audit_log" in p95.lower()
