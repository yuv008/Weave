"""Agent memory: remember and recall, re-gating on read, namespaces, conversations and the task queue.

Offline tests use the in-memory store with a controllable visibility set.
Live tests (WEAVE_LIVE_TESTS=1) run the Postgres store under a throwaway
tenant and delete their rows afterwards.
"""

import threading
import uuid

import pytest

from agents.gate.principal import Surface
from agents.memory import recall as gated
from agents.memory.store import ConversationNotFound, Episode, InMemoryMemoryStore, Task, terms
from agents.tools import memory as memory_tools
from agents.tools.base import ToolInputError
from agents.tools.invoke import invoke
from tests.answer_support import offline_runtime, principal
from tests.tool_support import live

D2 = "acme:decision:d2"
ITEM = "acme:slack:message:C0ENGSSO:1"
ROADMAP = "acme:github:issue:demo-org/roadmap:42"


@pytest.fixture()
def env(monkeypatch):
    env = offline_runtime()
    env.visible = {D2, ITEM, ROADMAP}

    def visible(ctx, ids):
        return {i for i in ids if i in env.visible}

    monkeypatch.setattr(gated, "visible_node_ids", visible)
    monkeypatch.setattr(memory_tools, "visible_node_ids", visible)
    return env


def ctx_for(env, who, surface=Surface.WEB):
    return env.runtime.context(principal(env, who, surface))


def test_remember_then_recall(env):
    ctx = ctx_for(env, "sam")
    saved = invoke("remember", ctx, {"note": "SSO provider follow-up", "node_ids": [D2, ITEM]})
    recalled = invoke("recall_memory", ctx, {"query": "what about the SSO provider"})
    [entry] = recalled.data["memories"]
    assert entry["memory_id"] == saved.data["memory_id"] and entry["note"] == "SSO provider follow-up"
    assert recalled.node_ids == [D2, ITEM]


def test_memory_holds_pointers_not_facts(env):
    ctx = ctx_for(env, "sam")
    with pytest.raises(ToolInputError, match="at most 200 characters"):
        invoke("remember", ctx, {"note": "x" * 201, "node_ids": [D2]})
    with pytest.raises(ToolInputError, match="not available"):
        invoke("remember", ctx, {"note": "n", "node_ids": [D2, "acme:decision:hidden"]})
    with pytest.raises(ToolInputError, match="node ids"):
        invoke("remember", ctx, {"note": "n", "node_ids": []})


def test_recall_drops_an_entry_once_the_caller_loses_access(env):
    ctx = ctx_for(env, "sam")
    invoke("remember", ctx, {"note": "the slack thread on SSO", "node_ids": [D2, ITEM]})
    invoke("remember", ctx, {"note": "roadmap issue on SSO", "node_ids": [ROADMAP]})
    env.visible.discard(ITEM)
    recalled = invoke("recall_memory", ctx, {"query": "SSO"})
    assert [m["note"] for m in recalled.data["memories"]] == ["roadmap issue on SSO"]
    assert recalled.hidden == 0 and "hidden" not in str(recalled.data)


def test_namespaces_do_not_mix(env):
    invoke("remember", ctx_for(env, "sam"), {"note": "Sam's SSO note", "node_ids": [D2]})
    assert invoke("recall_memory", ctx_for(env, "priya"), {"query": "SSO"}).data["memories"] == []
    other_agent = env.runtime.context(principal(env, "sam"), agent="mcp")
    assert invoke("recall_memory", other_agent, {"query": "SSO"}).data["memories"] == []


def test_nothing_is_recalled_or_kept_in_a_channel(env):
    invoke("remember", ctx_for(env, "sam"), {"note": "SSO note", "node_ids": [D2]})
    channel = env.resolver.for_channel("acme", "slack:C0ENGPUB", [env.fx.ids["sam"]], Surface.SLACK_CHANNEL)
    ctx = env.runtime.context(channel)
    assert invoke("recall_memory", ctx, {"query": "SSO"}).data["memories"] == []
    with pytest.raises(ToolInputError, match="private surface"):
        invoke("remember", ctx, {"note": "n", "node_ids": [D2]})


def test_conversation_turns_are_regated_and_owned(env):
    memory = env.runtime.memory
    sam = env.fx.ids["sam"]
    conv = memory.create_conversation("acme", sam, "web", "SSO")
    memory.add_turn(conv, "user", "Which SSO provider?", [])
    memory.add_turn(conv, "agent", "Use Provider B (thread).", [D2, ITEM])
    env.visible.discard(ITEM)
    turns = gated.history(ctx_for(env, "sam"), conv)
    assert [t.text for t in turns] == ["Which SSO provider?", gated.UNAVAILABLE_TURN]
    with pytest.raises(ConversationNotFound):
        gated.history(ctx_for(env, "priya"), conv)
    with pytest.raises(ConversationNotFound):
        gated.history(ctx_for(env, "sam"), "conv_missing")


def test_claim_task_never_hands_one_task_to_two_workers():
    store = InMemoryMemoryStore()
    ids = {store.add_task(Task("acme", "slack_dm", "ready", target_agent="slack_sender")) for _ in range(20)}
    claimed, lock = [], threading.Lock()

    def worker(name):
        while (task := store.claim_task("acme", "slack_dm", "slack_sender", name)) is not None:
            with lock:
                claimed.append(task.id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids)


def test_search_terms_drop_stop_words():
    assert terms("What is the SSO provider, and why?") == ["sso", "provider"]


@pytest.fixture()
def pg_store():
    from shared.env import load_env

    load_env()
    from agents.memory.store import PostgresMemoryStore
    from shared.pg import connect_pg

    tenant = f"test_{uuid.uuid4().hex[:12]}"
    yield tenant, PostgresMemoryStore(connect_pg)
    conn = connect_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conversation_turns WHERE conversation_id IN "
                        "(SELECT id FROM conversations WHERE tenant_id = %s)", (tenant,))
            for table in ("conversations", "episodes", "tasks", "playbooks"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
        conn.commit()
    finally:
        conn.close()


@live
def test_postgres_episodes_search_and_feedback(pg_store):
    tenant, store = pg_store
    mine = store.add_episode(Episode(tenant, "p1", "answer", question="Which SSO provider should I use?",
                                     node_ids=[D2, ITEM], tools=[{"tool": "get_decision"}]))
    store.add_episode(Episode(tenant, "p1", "answer", question="What is the Q4 budget?", node_ids=["x"]))
    store.add_episode(Episode(tenant, "p2", "answer", question="SSO provider for p2", node_ids=[D2]))
    found = store.search_episodes(tenant, "p1", "answer", "sso provider", 5)
    assert [e.id for e in found] == [mine] and found[0].node_ids == [D2, ITEM]
    assert len(store.search_episodes(tenant, "p1", "answer", "", 5)) == 2
    store.set_feedback(mine, {"decision": "accept"})
    assert store.get_episode(mine).feedback == {"decision": "accept"}


@live
def test_postgres_conversations_playbooks_and_tasks(pg_store):
    tenant, store = pg_store
    conv = store.create_conversation(tenant, "p1", "web", "SSO")
    store.add_turn(conv, "user", "q", [])
    store.add_turn(conv, "agent", "a", [D2])
    assert [(t.role, t.node_ids) for t in store.turns(conv, 10)] == [("user", []), ("agent", [D2])]
    assert store.get_conversation(conv).person_id == "p1"
    assert store.put_playbook(tenant, "sales", "draft", ["decision"], "test") == 1
    assert store.put_playbook(tenant, "sales", "draft", ["dependencies", "propose"], "test") == 2
    assert store.playbook(tenant, "sales", "draft") == ["dependencies", "propose"]
    task = Task(tenant, "proposal_email", "proposed", payload={"draft": "d"}, created_by="p1")
    store.add_task(task)
    store.update_task(task.id, state="accepted", payload={"decision": "accept"})
    got = store.get_task(task.id)
    assert got.state == "accepted" and got.payload == {"draft": "d", "decision": "accept"}


@live
def test_postgres_claim_task_skips_locked_rows(pg_store):
    tenant, store = pg_store
    ids = {store.add_task(Task(tenant, "slack_dm", "ready", target_agent="slack_sender")) for _ in range(8)}
    claimed, lock = [], threading.Lock()

    def worker(name):
        while (task := store.claim_task(tenant, "slack_dm", "slack_sender", name)) is not None:
            with lock:
                claimed.append(task.id)

    threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(3)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert sorted(claimed) == sorted(ids) and len(claimed) == len(set(claimed))
