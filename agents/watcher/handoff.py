"""Handoff: a watcher's ``draft_customer_update`` task becomes a draft for its recipient.

The task carries ids only. At run time the recipient is resolved again, every
id is re-gated as that person, and the question is built from the names they
may see. The answer agent then drafts from current facts with the normal draft
playbook, so the result is a ``proposal_email`` created by the recipient in
their own episode, waiting for them to confirm. No notice text is reused.
"""

import logging

from agents.answer.agent import AnswerAgent
from agents.answer.intents import Intent
from agents.answer.subjects import first, subjects_for
from agents.gate.principal import Surface
from agents.gate.resolve import NotAuthorized
from agents.memory.store import Task
from agents.runtime import Runtime
from agents.tools.base import ToolContext
from agents.tools.common import supersession, visible_node_ids
from shared.schema import Label, Prop, Rel

log = logging.getLogger("weave.watcher")

DRAFT_CUSTOMER_UPDATE = "draft_customer_update"
ANSWER = "answer"

_DEPENDS_ON = f"""
UNWIND $ids AS cid
MATCH (c {{{Prop.ID}: cid}})-[:{Rel.DEPENDS_ON}]->(n)
RETURN DISTINCT n.{Prop.ID}
"""

_TOPICS = f"""
UNWIND $ids AS did
MATCH (d:{Label.DECISION} {{{Prop.ID}: did}})-[:{Rel.ABOUT}]->(t:{Label.TOPIC})
RETURN DISTINCT t.{Prop.ID}
"""


def _ids(payload: dict, key: str) -> list[str]:
    values = payload.get(key)
    if not isinstance(values, list):
        return []
    return list(dict.fromkeys(v for v in values if isinstance(v, str) and v))


def _changed_fact(change) -> str | None:
    if not isinstance(change, str):
        return None
    fact, _, kind = change.rpartition(":")
    return fact if fact and kind else None


def subject_ids(ctx: ToolContext, commitments: list[str], customers: list[str], deals: list[str],
                change: str | None) -> list[str]:
    """Customers and deals, then the topics and projects the commitments rest on, as the caller sees them.

    Topics come from visible decisions the commitments depend on and from
    visible decisions that replaced the changed fact; hidden ones add nothing.
    """

    depends = [row[0] for row in ctx.rows(_DEPENDS_ON, ids=commitments)] if commitments else []
    visible = sorted(visible_node_ids(ctx, depends))
    fact = _changed_fact(change)
    newer = [other for other, restricted in supersession(ctx, Label.DECISION, fact, older=False)[0]
             if restricted is None] if fact else []
    decisions = [*visible, *newer]
    topics = [row[0] for row in ctx.rows(_TOPICS, ids=decisions)] if decisions else []
    return list(dict.fromkeys([*customers, *deals, *topics, *visible]))


def question_for(subjects) -> str:
    who = first(subjects, Label.CUSTOMER, Label.DEAL)
    what = first(subjects, Label.TOPIC, Label.PROJECT)
    if who and what:
        return f"Draft a status update for {who.name} on {what.name}"
    if who:
        return f"Draft a status update for {who.name}"
    if what:
        return f"Draft a status update on {what.name}"
    return "Draft a status update on my commitment"


def _finish(runtime: Runtime, task: Task, state: str, **payload) -> dict:
    runtime.memory.update_task(task.id, state=state, payload=payload)
    return {"task_id": task.id, "recipient": task.payload.get("recipient"), "state": state, **payload}


def handle(runtime: Runtime, agent: AnswerAgent, task: Task) -> dict:
    """Run one claimed handoff as its recipient. Returns the outcome; the task ends done, expired or failed."""

    try:
        return _run(runtime, agent, task)
    except Exception as exc:
        log.warning("handoff %s failed: %s", task.id, type(exc).__name__)
        return _finish(runtime, task, "failed", reason=f"error: {type(exc).__name__}")


def _run(runtime: Runtime, agent: AnswerAgent, task: Task) -> dict:
    payload = task.payload or {}
    recipient = payload.get("recipient")
    if task.kind != DRAFT_CUSTOMER_UPDATE:
        return _finish(runtime, task, "failed", reason="not a customer update handoff")
    if not isinstance(recipient, str) or not recipient:
        return _finish(runtime, task, "failed", reason="no recipient")
    try:
        principal = runtime.resolver.for_person(task.tenant, recipient, Surface.SLACK_DM)
    except NotAuthorized:
        return _finish(runtime, task, "expired", reason="recipient unknown")

    ctx = runtime.context(principal, agent=agent.agent)
    commitments, customers, deals = (_ids(payload, k) for k in ("commitment_ids", "customer_ids", "deal_ids"))
    visible = visible_node_ids(ctx, [*commitments, *customers, *deals])
    commitments = [c for c in commitments if c in visible]
    if not commitments:
        return _finish(runtime, task, "expired", reason="no commitment is visible to the recipient")
    customers = [c for c in customers if c in visible]
    deals = [d for d in deals if d in visible]

    subjects = subjects_for(ctx, subject_ids(ctx, commitments, customers, deals, payload.get("change")))
    card = agent.answer(principal, question_for(subjects), intent=Intent.DRAFT,
                        subject_ids=[m.id for m in subjects], fact_ids=commitments)
    proposal = card.proposal or {}
    if proposal.get("tool") != "draft_email" or not proposal.get("proposal_id"):
        return _finish(runtime, task, "failed", reason="no draft was produced", episode_id=card.episode_id)
    return _finish(runtime, task, "done", proposal_id=proposal["proposal_id"], episode_id=card.episode_id)


def run_handoffs(runtime: Runtime, agent: AnswerAgent, tenant: str, worker: str, limit: int = 50) -> list[dict]:
    """Claim and run up to ``limit`` ready handoffs (SKIP LOCKED in Postgres)."""

    outcomes = []
    while len(outcomes) < limit:
        task = runtime.memory.claim_task(tenant, DRAFT_CUSTOMER_UPDATE, ANSWER, worker)
        if task is None:
            break
        outcomes.append(handle(runtime, agent, task))
    return outcomes
