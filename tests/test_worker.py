import logging

import pytest

from app.ingest.records import MemoryStore
from app.ingest.state import channel_sync_id, failure_attempts, failure_id, record_failure
from app.ingest.worker import backfill_public_channels, retry_failures

TENANT = "tenant1"
CONNECTION = "conn1"
WORKSPACE = "T1"
CHANNEL = "C0ENGSSO"


def _message(text, ts, **extra):
    payload = {"type": "message", "user": "U1", "text": text, "ts": ts}
    payload.update(extra)
    return payload


def _page(messages, cursor=""):
    return {"ok": True, "messages": messages, "response_metadata": {"next_cursor": cursor}}


def _channels(*channels):
    return {"ok": True, "channels": list(channels), "response_metadata": {"next_cursor": ""}}


def _public(channel_id=CHANNEL):
    return {"id": channel_id, "is_channel": True, "is_private": False}


class Listing:
    def __init__(self, channels):
        self.channels = channels
        self.history_calls = []
        self.reply_calls = []

    def list_public_channels(self, cursor=None):
        return _channels(*self.channels)


class TwoPages(Listing):
    def __init__(self):
        super().__init__([_public()])
        self.stop = True

    def history(self, channel_id, cursor=None, oldest=None):
        self.history_calls.append((cursor, oldest))
        if cursor == "page-2":
            if self.stop:
                raise RuntimeError("stop after page 1")
            return _page([_message("oldest home", "50.000100")])
        return _page(
            [_message("newer home", "200.000100"), _message("middle home", "100.000100")],
            cursor="page-2",
        )


class BoomGraph:
    def __init__(self, inner):
        self.inner = inner

    def query(self, query, params=None):
        if params and params.get("excerpt") == "two" and "RETURN do_apply" in query:
            raise RuntimeError("boom")
        return self.inner.query(query, params)


class OnePage(Listing):
    def __init__(self, messages):
        super().__init__([_public()])
        self.messages = messages

    def history(self, channel_id, cursor=None, oldest=None):
        self.history_calls.append((cursor, oldest))
        return _page(self.messages, cursor="next")


def _backfill(client, graph, store, **kwargs):
    backfill_public_channels(
        TENANT,
        CONNECTION,
        WORKSPACE,
        client,
        graph,
        workspace_domain="acme.slack.com",
        store=store,
        **kwargs,
    )


def _sync(store):
    row = store.get(channel_sync_id(TENANT, WORKSPACE, CHANNEL))
    if not row:
        return None
    payload = row["payload"]
    return payload.get("oldest") or "0", payload.get("history_cursor") or "", payload.get("status") or ""


def _message_count(graph):
    return graph.query(
        "MATCH (m:Message) WHERE m.tenant_id = $tenant RETURN count(m)",
        {"tenant": TENANT},
    ).result_set[0][0]


def test_checkpoint_waits_for_the_cursor_then_a_second_run_is_idle(graph):
    client = TwoPages()
    store = MemoryStore()
    with pytest.raises(RuntimeError, match="stop after page 1"):
        _backfill(client, graph, store)

    oldest, cursor, status = _sync(store)
    assert oldest == "0"
    assert cursor == "page-2"
    assert status == "in_progress"

    client.stop = False
    _backfill(client, graph, store)
    oldest, cursor, status = _sync(store)
    assert oldest == "200.000100"
    assert cursor == ""
    assert status == "complete"
    assert _message_count(graph) == 3
    calls_after_finish = len(client.history_calls)

    _backfill(client, graph, store)
    assert len(client.history_calls) == calls_after_finish
    assert _message_count(graph) == 3


def test_graph_error_leaves_the_cursor_and_keeps_the_first_message(graph):
    client = OnePage([_message("one", "10.1"), _message("two", "10.2")])
    store = MemoryStore()
    with pytest.raises(RuntimeError, match="boom"):
        _backfill(client, BoomGraph(graph), store)

    assert _sync(store) is None
    rows = graph.query(
        "MATCH (m:Message) WHERE m.tenant_id = $tenant RETURN m.excerpt ORDER BY m.excerpt",
        {"tenant": TENANT},
    ).result_set
    assert rows == [["one"]]


def test_private_channel_is_never_upserted(graph):
    client = Listing([{"id": "G1", "is_private": True, "is_channel": False}])
    client.history = lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("history called"))

    _backfill(client, graph, MemoryStore())

    assert _message_count(graph) == 0


def test_thread_crash_replays_the_remaining_reply_page(graph):
    parent = _message("parent", "300.000100", reply_count=2)
    reply_one = _message("reply one", "300.000200", thread_ts="300.000100")
    reply_two = _message("reply two", "300.000300", thread_ts="300.000100")

    class Threads(Listing):
        def __init__(self):
            super().__init__([_public()])
            self.crash = True
            self.history_calls = []

        def history(self, channel_id, cursor=None, oldest=None):
            self.history_calls.append(cursor)
            return _page([parent])

        def replies(self, channel_id, parent_ts, cursor=None):
            self.reply_calls.append(cursor)
            if self.crash and cursor == "r2":
                raise RuntimeError("crash")
            if cursor == "r2":
                return _page([reply_two])
            return _page([parent, reply_one], cursor="r2")

    client = Threads()
    store = MemoryStore()
    with pytest.raises(RuntimeError, match="crash"):
        _backfill(client, graph, store)
    assert _sync(store) is None

    client.crash = False
    _backfill(client, graph, store)

    assert _message_count(graph) == 3
    replies = graph.query(
        """
        MATCH (:Message)-[:REPLIES_TO]->(parent:Message {id: $id})
        RETURN count(parent)
        """,
        {"id": f"{TENANT}:slack:message:{CHANNEL}:300.000100"},
    ).result_set
    assert replies[0][0] == 2


def test_retry_fetches_one_message_without_history(graph):
    class RetryClient:
        def __init__(self):
            self.history_calls = 0
            self.fetch_calls = 0

        def history(self, *args, **kwargs):
            self.history_calls += 1
            raise AssertionError("history called")

        def fetch_message(self, channel_id, ts):
            self.fetch_calls += 1
            return {"messages": [_message("recovered", ts)]}

    store = MemoryStore()
    record_failure(
        store,
        node_id=failure_id(TENANT, CHANNEL, "10.1"),
        fields={
            "node_id": f"{TENANT}:slack:message:{CHANNEL}:10.1",
            "version": "0000000000000010.100000",
            "error_class": "ValueError",
            "tenant_id": TENANT,
            "connection_id": CONNECTION,
            "workspace_id": WORKSPACE,
            "channel_id": CHANNEL,
            "message_ts": "10.1",
            "workspace_domain": "acme.slack.com",
        },
    )
    client = RetryClient()
    retry_failures(graph, client, store=store)

    assert client.history_calls == 0
    assert client.fetch_calls == 1
    rows = graph.query(
        "MATCH (m:Message {id: $id}) RETURN m.excerpt",
        {"id": f"{TENANT}:slack:message:{CHANNEL}:10.1"},
    ).result_set
    assert rows == [["recovered"]]
    assert store.get(failure_id(TENANT, CHANNEL, "10.1")) is None


def test_fifth_failure_stays_on_the_list(graph):
    class Broken:
        def __init__(self):
            self.calls = 0

        def fetch_message(self, channel_id, ts):
            self.calls += 1
            return {"messages": [{"text": 1, "user": "U1"}]}

    node = failure_id(TENANT, CHANNEL, "10.1")
    fields = {
        "node_id": f"{TENANT}:slack:message:{CHANNEL}:10.1",
        "version": "0000000000000010.100000",
        "error_class": "ValueError",
        "tenant_id": TENANT,
        "connection_id": CONNECTION,
        "workspace_id": WORKSPACE,
        "channel_id": CHANNEL,
        "message_ts": "10.1",
        "workspace_domain": "",
    }
    store = MemoryStore()
    for _ in range(4):
        record_failure(store, node_id=node, fields=fields)
    assert failure_attempts(store, node) == 4

    client = Broken()
    retry_failures(graph, client, store=store)
    assert client.calls == 1
    assert failure_attempts(store, node) == 5

    retry_failures(graph, client, store=store)
    assert client.calls == 1


def test_backfill_logs_omit_message_text(graph, caplog):
    secret = "xoxb-unique-token-and-body"
    client = OnePage([_message(secret, "10.1")])
    client.history = lambda channel_id, cursor=None, oldest=None: _page([_message(secret, "10.1")])

    with caplog.at_level(logging.INFO, logger="weave.ingest"):
        _backfill(client, graph, MemoryStore())

    assert secret not in caplog.text
    assert "tenant_id=tenant1" in caplog.text
