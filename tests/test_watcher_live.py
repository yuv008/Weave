"""Flow 3 on a scratch graph with Neon memory (WEAVE_LIVE_TESTS=1).

The fixture is built under a throwaway tenant, so the worker's task claims
never reach real ``acme`` tasks. One tick runs events, handoffs and sends;
then the same tick again, then a replay of the event. Every Neon row the
run wrote (tasks, episodes, conversations, playbooks) is deleted afterwards;
the audit goes to memory.
"""

import uuid
from types import SimpleNamespace

import pytest

from agents.answer.agent import build_agent
from agents.gate.principal import Surface
from agents.graph import ReadOnlyGraph
from agents.mcp.audit import MemoryAudit
from agents.runtime import Runtime
from agents.tools.common import visible_node_ids
from agents.watcher.outbox import MemoryOutbox
from agents.watcher.sender import DRAFT_POINTER
from agents.watcher.watcher import handoff_id, notice_id
from agents.watcher.worker import tick
from scripts.seed_playbooks import PLAYBOOKS
from shared.config import load_config
from shared.schema import Prop, Rel
from tests.gate_support import NOW, fixture_resolver
from tests.tool_support import live

pytestmark = live


def _tasks(connect, tenant):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("SELECT id, state FROM tasks WHERE tenant_id = %s ORDER BY id", (tenant,))
            return cur.fetchall()
    finally:
        conn.close()


def _cleanup(connect, tenant):
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM conversation_turns WHERE conversation_id IN "
                        "(SELECT id FROM conversations WHERE tenant_id = %s)", (tenant,))
            for table in ("conversations", "episodes", "tasks", "playbooks"):
                cur.execute(f"DELETE FROM {table} WHERE tenant_id = %s", (tenant,))
        conn.commit()
    finally:
        conn.close()


@pytest.fixture(scope="module")
def flow3():
    from shared.env import load_env

    load_env()
    from agents.memory.store import PostgresMemoryStore
    from fixtures.acme import build
    from fixtures.seed import seed_graph
    from shared.graphdb import connect
    from shared.pg import connect_pg

    tenant = f"test{uuid.uuid4().hex[:12]}"
    fx = build(tenant)
    name = f"co:test_{uuid.uuid4().hex}"
    raw = connect().select_graph(name)
    try:
        seed_graph(raw, fx)
        graph = ReadOnlyGraph(raw, name)
        resolver, _ = fixture_resolver(fx)
        memory = PostgresMemoryStore(connect_pg)
        for role, intent, steps in PLAYBOOKS:
            memory.put_playbook(tenant, role, str(intent), steps, "test")
        runtime = Runtime(lambda t: graph, load_config(), resolver=resolver, clock=lambda: NOW,
                          pg_connect=connect_pg, audit=MemoryAudit(), memory=memory)
        outbox = MemoryOutbox.from_fixture(fx)
        [event] = outbox.pending(tenant)
        agent = build_agent(runtime)

        first = tick(runtime, tenant, outbox=outbox, agent=agent, worker="test-watcher")
        after_first = _tasks(connect_pg, tenant)
        second = tick(runtime, tenant, outbox=outbox, agent=agent, worker="test-watcher")
        after_second = _tasks(connect_pg, tenant)
        outbox.add(event)
        replay = tick(runtime, tenant, outbox=outbox, agent=agent, worker="test-watcher")
        after_replay = _tasks(connect_pg, tenant)

        priya = fx.ids["priya"]
        yield SimpleNamespace(
            fx=fx, tenant=tenant, runtime=runtime, outbox=outbox, event=event, priya=priya,
            principal=resolver.for_person(tenant, priya, Surface.SLACK_DM),
            first=first, second=second, replay=replay,
            after_first=after_first, after_second=after_second, after_replay=after_replay,
            notice=memory.get_task(notice_id(event.id, priya)), handoff=memory.get_task(handoff_id(event.id, priya)),
        )
    finally:
        raw.delete()
        _cleanup(connect_pg, tenant)


def eng_sso_leaks(fx) -> list[str]:
    """Every id, link, quote and excerpt from #eng-sso: none of it may reach Priya."""

    items = [e.src for e in fx.edges if e.rel == Rel.IN_SOURCE and e.dst == fx.ids["src_eng_sso"]]
    quotes = [e.props["quote"] for e in fx.edges if e.rel == Rel.EVIDENCED_BY and e.dst in items]
    nodes = [fx.node(i).props for i in items]
    return [*items, *(p[Prop.SOURCE_URL] for p in nodes), *(p[Prop.EXCERPT] for p in nodes), *quotes,
            fx.ids["src_eng_sso"], "C0ENGSSO", "#eng-sso"]


def assert_no_leak(fx, text: str) -> None:
    assert "Provider A" not in text
    leaked = [s for s in eng_sso_leaks(fx) if s in text]
    assert leaked == []


def test_only_priya_is_notified(flow3):
    [outcome] = flow3.first.events
    assert outcome.event_id == flow3.event.id and not outcome.error
    assert outcome.notified == [flow3.priya] and outcome.filtered == 0
    assert sorted(outcome.written) == sorted([notice_id(flow3.event.id, flow3.priya),
                                              handoff_id(flow3.event.id, flow3.priya)])
    rows = [r for r in flow3.runtime.audit.rows if r["kind"] == "watcher"]
    assert [(r["principal"].user_id, r["outcome"]) for r in rows] == [(flow3.priya, "notified")] * 2
    assert all(r["event_id"] == flow3.event.id and r["tools"] == ["impact_of"] for r in rows)
    assert_no_leak(flow3.fx, " ".join(i for r in rows for i in r["node_ids"]))


def test_notice_states_her_commitment_and_the_replacement_only(flow3):
    notice, fx = flow3.notice, flow3.fx
    text = notice.payload["text"]
    assert "SSO will be live for Acme by Nov 15" in text and "Provider B" in text
    assert text.startswith("A decision your work depends on was replaced.")
    assert_no_leak(fx, text)
    assert_no_leak(fx, str(notice.payload))
    assert_no_leak(fx, " ".join(notice.node_ids))


def test_notice_evidence_is_visible_to_priya(flow3):
    fx, evidence = flow3.fx, flow3.notice.payload["evidence_ids"]
    assert evidence == [fx.ids["commitment_acme_sso"], fx.ids["decision_provider_b"]]
    ctx = flow3.runtime.context(flow3.principal, agent="impact_watcher")
    assert visible_node_ids(ctx, evidence) == set(evidence)


def test_one_handoff_drafts_a_customer_update_as_priya(flow3):
    fx, handoff = flow3.fx, flow3.handoff
    assert [h["task_id"] for h in flow3.first.handoffs] == [handoff.id]
    assert handoff.state == "done" and handoff.payload["recipient"] == flow3.priya
    assert handoff.payload["customer_ids"] == [fx.ids["customer_acme"]]
    assert handoff.payload["commitment_ids"] == [fx.ids["commitment_acme_sso"]]
    assert_no_leak(fx, str(handoff.payload))

    memory = flow3.runtime.memory
    proposal = memory.get_task(handoff.payload["proposal_id"])
    assert proposal.kind == "proposal_email" and proposal.state == "proposed"
    assert proposal.created_by == flow3.priya and proposal.tenant == flow3.tenant
    draft = proposal.payload["draft"]
    assert "SSO will be live for Acme by Nov 15." in draft and "Use Provider B for SSO." in draft
    assert fx.node_by_key("item_roadmap_issue").props[Prop.SOURCE_URL] in draft
    assert_no_leak(fx, draft)
    assert_no_leak(fx, " ".join(proposal.node_ids))
    episode = memory.get_episode(handoff.payload["episode_id"])
    assert episode.person_id == flow3.priya and episode.intent == "draft"
    assert proposal.episode_id == episode.id


def test_dry_run_sends_the_notice_with_the_draft_pointer(flow3):
    notice = flow3.notice
    assert [s["task_id"] for s in flow3.first.sends] == [notice.id]
    delivery = notice.payload["delivery"]
    assert notice.state == "sent" and delivery["transport"] == "dry_run" and delivery["to"] == flow3.priya
    pointer = DRAFT_POINTER.format(proposal_id=flow3.handoff.payload["proposal_id"])
    assert delivery["text"] == f"{notice.payload['text']}\n\n{pointer}"
    assert_no_leak(flow3.fx, delivery["text"])


def test_event_is_processed_and_running_again_adds_nothing(flow3):
    assert flow3.outbox.pending(flow3.tenant) == []
    assert (flow3.second.events, flow3.second.handoffs, flow3.second.sends) == ([], [], [])
    assert flow3.after_second == flow3.after_first
    [again] = flow3.replay.events
    assert again.written == [] and sorted(again.existing) == sorted(flow3.first.events[0].written)
    assert flow3.replay.handoffs == [] and flow3.replay.sends == []
    assert flow3.after_replay == flow3.after_first
    assert sorted(state for _, state in flow3.after_first) == ["done", "proposed", "sent"]
