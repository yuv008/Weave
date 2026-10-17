"""The answer agent on the scratch graph (WEAVE_LIVE_TESTS=1): flows 1 and 2, provenance, impact, notes, audit."""

import pytest

from agents.answer.actions import ActionNotFound, confirm_action
from agents.answer.agent import build_agent
from agents.gate.principal import Surface
from agents.memory.store import InMemoryMemoryStore
from agents.mcp.audit import MemoryAudit
from agents.runtime import Runtime
from agents.tools.base import ToolInputError
from agents.tools.invoke import invoke
from scripts.seed_playbooks import seed
from tests.answer_support import rule_config
from tests.gate_support import NOW, fixture_resolver
from tests.tool_support import live, scratch  # noqa: F401

pytestmark = live


@pytest.fixture()
def env(scratch):
    from shared.pg import connect_pg

    fx, _, graph = scratch
    resolver, store = fixture_resolver(fx)
    memory = InMemoryMemoryStore(clock=lambda: NOW)
    seed(memory)
    runtime = Runtime(lambda tenant: graph, rule_config(), resolver=resolver, clock=lambda: NOW,
                      pg_connect=connect_pg, audit=MemoryAudit(), memory=memory)
    return fx, runtime, build_agent(runtime), resolver, store


def person(env, who):
    fx, _, _, resolver, _ = env
    return resolver.for_person(fx.tenant, fx.ids[who], Surface.WEB)


def tools(card):
    return [s["tool"] for s in card.trace if s["status"] == "ok"]


def test_flow1_sam_gets_provider_b_with_the_slack_thread(env):
    fx, runtime, agent, *_ = env
    card = agent.answer(person(env, "sam"), "Which SSO provider should I use?")
    assert card.intent == "lookup" and card.answer.startswith("Use Provider B for SSO.")
    slack = card.evidence[fx.ids["item_eng_switch_b"]]
    assert slack["author"] == "Dev" and "slack.com" in slack["url"] and slack["quote"]
    assert any("Provider A" in f and "Oct 3" in f for f in card.freshness)
    assert card.confidence == 0.9 and not card.likely
    assert card.path and all(e.src and e.rel and e.dst for e in card.path)
    assert fx.ids["item_eng_switch_b"] in card.claims[0].evidence_ids


def test_priya_sees_the_same_decision_on_roadmap_evidence_only(env):
    fx, _, agent, *_ = env
    card = agent.answer(person(env, "priya"), "Which SSO provider should I use?")
    assert card.answer.startswith("Use Provider B for SSO.")
    assert set(card.evidence) == {fx.ids["item_roadmap_issue"]}
    assert fx.ids["item_eng_switch_b"] not in str(card.public())
    assert "hidden_count" not in card.public()


def test_provenance_questions_trace_the_decision(env):
    fx, _, agent, *_ = env
    card = agent.answer(person(env, "sam"), "Why did we switch SSO providers?")
    assert card.intent == "provenance"
    assert tools(card)[:3] == ["recall_memory", "get_decision", "trace_decision"]
    trace_step = next(s for s in card.trace if s["tool"] == "trace_decision")
    assert trace_step["args"]["decision_id"] == fx.ids["decision_provider_b"]
    assert any(c.kind == "provenance" for c in card.claims)


def test_priya_impact_question_returns_the_acme_commitment(env):
    fx, _, agent, *_ = env
    card = agent.answer(person(env, "priya"), "What depends on the SSO integration?")
    assert card.intent == "impact" and "get_dependencies" in tools(card)
    assert any(c.node_id == fx.ids["commitment_acme_sso"] for c in card.claims)
    assert "SSO will be live for Acme by Nov 15." in card.answer + " ".join(c.text for c in card.claims)


def test_flow2_priya_draft_cites_roadmap_and_projection_never_the_thread(env):
    fx, runtime, agent, *_ = env
    card = agent.answer(person(env, "priya"), "Draft a status update for Acme about SSO")
    assert card.intent == "draft" and tools(card)[1] == "get_dependencies"
    proposal = card.proposal
    assert proposal["tool"] == "draft_email" and proposal["state"] == "proposed" and proposal["sent"] is False
    draft = proposal["draft"]
    assert "Use Provider B for SSO." in draft and "github.com/demo-org/roadmap/issues/42" in draft
    assert "Restricted summary" in draft and "slack.com" not in draft
    task = runtime.memory.get_task(proposal["proposal_id"])
    assert fx.ids["projection_provider_switch"] in task.node_ids
    assert fx.ids["item_eng_switch_b"] not in task.node_ids

    outcome = confirm_action(runtime, person(env, "priya"), task.id, "accept")
    assert outcome["state"] == "accepted" and outcome["sent"] is False
    assert runtime.memory.get_episode(card.episode_id).feedback["decision"] == "accept"
    with pytest.raises(ToolInputError, match="already accepted"):
        confirm_action(runtime, person(env, "priya"), task.id, "accept")
    with pytest.raises(ActionNotFound):
        confirm_action(runtime, person(env, "sam"), task.id, "reject")


def test_notify_is_refused_when_the_recipient_cannot_see_the_evidence(env):
    fx, runtime, *_ = env
    ctx = runtime.context(person(env, "sam"))
    invoke("get_decision", ctx, {"topic": "SSO"})
    with pytest.raises(ToolInputError, match="cannot see any of that evidence"):
        invoke("notify_owner", ctx, {"colleague": fx.ids["priya"], "message": "see the thread",
                                     "evidence_ids": [fx.ids["item_eng_switch_b"]]})


def test_notify_with_roadmap_evidence_queues_a_slack_dm_on_confirm(env):
    fx, runtime, agent, *_ = env
    card = agent.answer(person(env, "priya"), "Let Sam know about the SSO provider change")
    proposal = card.proposal
    assert proposal["tool"] == "notify_owner" and proposal["recipient"] == fx.ids["sam"]
    assert set(card.evidence) <= {fx.ids["item_roadmap_issue"], fx.ids["projection_provider_switch"]}
    outcome = confirm_action(runtime, person(env, "priya"), proposal["proposal_id"], "edit",
                             "Heads up: SSO moved to Provider B (roadmap #42).")
    dm = runtime.memory.get_task(outcome["queued_task_id"])
    assert dm.kind == "slack_dm" and dm.state == "ready" and dm.target_agent == "slack_sender"
    assert dm.payload["recipient"] == fx.ids["sam"] and dm.payload["text"].startswith("Heads up")
    assert dm.parent_id == proposal["proposal_id"]
    assert runtime.memory.claim_task("acme", "slack_dm", "slack_sender", "w1").id == dm.id
    assert runtime.memory.get_episode(card.episode_id).feedback["decision"] == "edit"


def test_one_answer_audit_row_per_answer(env):
    _, runtime, agent, *_ = env
    before = len([r for r in runtime.audit.rows if r["kind"] == "answer"])
    agent.answer(person(env, "dev"), "Who owns the SSO integration?")
    agent.answer(person(env, "rohan"), "What is the budget for SSO integration?")
    rows = [r for r in runtime.audit.rows if r["kind"] == "answer"][before:]
    assert [r["intent"] for r in rows] == ["ownership", "lookup"]
    assert all(r["tools"][0] == "recall_memory" and r["outcome"] == "ok" for r in rows)


def test_ownership_cites_the_owns_edge(env):
    fx, _, agent, *_ = env
    card = agent.answer(person(env, "dev"), "Who owns the SSO integration?")
    owner = next(c for c in card.claims if c.kind == "owner")
    assert owner.text.startswith("Dev") and owner.evidence_ids[0].startswith("path:")
    assert card.evidence[owner.evidence_ids[0]]["rel"] == "OWNS"


def test_recalled_memory_is_regated_after_revocation(env):
    fx, runtime, agent, resolver, store = env
    agent.answer(person(env, "sam"), "Which SSO provider should I use?")
    ctx = runtime.context(person(env, "sam"))
    assert invoke("recall_memory", ctx, {"query": "SSO provider"}).data["memories"]
    store.members[fx.ids["sam"]].discard("slack:C0ENGSSO")
    store.epoch += 1
    try:
        ctx = runtime.context(person(env, "sam"))
        assert invoke("recall_memory", ctx, {"query": "SSO provider"}).data["memories"] == []
    finally:
        store.members[fx.ids["sam"]].add("slack:C0ENGSSO")
        store.epoch += 1
