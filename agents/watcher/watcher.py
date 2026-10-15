"""ImpactWatcher: one change event becomes a gated notice and a handoff per affected owner.

Recipients come from ``affected_owners``, which is ungated and only picks
whom to ask; that list is never shown. Each recipient's notice is built only
from their own ``impact_of`` result, run with their own principal: a root
they cannot see reads "a decision your work depends on", and its statement,
evidence and links never reach them. The watcher reads the graph and writes
only tasks and audit rows. Task ids are deterministic, so re-running an event
after a crash writes nothing new.
"""

import logging
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, field

from agents.gate.principal import Principal, Surface
from agents.gate.resolve import NotAuthorized
from agents.memory.store import Task
from agents.runtime import Runtime
from agents.tools.base import ToolContext
from agents.tools.common import visible_node_ids
from agents.tools.dependencies import CHANGE_KINDS, affected_owners
from agents.tools.invoke import invoke
from agents.watcher.outbox import ChangeEvent, Outbox
from shared.schema import CLAIM_LABELS, Label, Progress, Prop, Rel, Status

log = logging.getLogger("weave.watcher")

AGENT = "impact_watcher"
SLACK_DM = "slack_dm"
SLACK_SENDER = "slack_sender"
DRAFT_CUSTOMER_UPDATE = "draft_customer_update"
ANSWER = "answer"
TOOLS = ["impact_of"]

_CHANGED = {
    "superseded": "was replaced",
    "retracted": "was withdrawn",
    "contradicts": "is contradicted by newer information",
}
_NOUNS = {
    Label.DECISION: "decision", Label.FIGURE: "figure", Label.COMMITMENT: "commitment",
    Label.PROJECT: "project", Label.DEAL: "deal",
}

_CUSTOMERS = f"""
UNWIND $ids AS cid
MATCH (:{Label.COMMITMENT} {{{Prop.ID}: cid}})-[:{Rel.PROMISED_TO}]->(k:{Label.CUSTOMER})
RETURN DISTINCT k.{Prop.ID}
"""

_DEALS = f"""
UNWIND $ids AS kid
MATCH (d:{Label.DEAL})-[:{Rel.FOR}]->(:{Label.CUSTOMER} {{{Prop.ID}: kid}})
RETURN DISTINCT d.{Prop.ID}
"""


def notice_id(event_id: str, person_id: str) -> str:
    return f"notice:{event_id}:{person_id}"


def handoff_id(event_id: str, person_id: str) -> str:
    return f"handoff:{event_id}:{person_id}"


@dataclass
class WatchOutcome:
    event_id: str
    change: str
    notified: list[str] = field(default_factory=list)
    """Recipients who got a notice."""
    filtered: int = 0
    """Candidates left with nothing of their own to hear about. Counted, never named."""
    written: list[str] = field(default_factory=list)
    """Task ids this run wrote."""
    existing: list[str] = field(default_factory=list)
    """Task ids already written by an earlier run of the same event."""
    error: bool = False


def owned_by(affected: list[dict], person_id: str) -> list[dict]:
    """Affected work the person owns that is still live: no superseded claims, no finished commitments."""

    return [
        n for n in affected
        if any(o.get("id") == person_id for o in n.get("owners", []))
        and not (n.get("label") in CLAIM_LABELS and n.get("status") != Status.ACTIVE)
        and n.get("progress") != Progress.DONE
    ]


def compose_notice(kind: str, root: dict, owned: list[dict], replaced_by: list[dict]) -> str:
    """The notice, from one recipient's ``impact_of`` result only. A restricted root is never stated."""

    noun = _NOUNS.get(root.get("label"), "fact")
    changed = _CHANGED.get(kind, "changed")
    if root.get("restricted") or not root.get("name"):
        lines = [f"A {noun} your work depends on {changed}."]
    else:
        lines = [f'The {noun} "{root["name"]}", which your work depends on, {changed}.']
    if root.get("restricted") and root.get("summary"):
        lines.append(f"What changed: {root['summary']}")
    for node in owned:
        if node.get("label") == Label.COMMITMENT:
            due = f" (due {node['due']})" if node.get("due") else ""
            lines.append(f'Your commitment "{node["name"]}"{due} is at risk.')
        else:
            lines.append(f'Your {_NOUNS.get(node.get("label"), "work")} "{node["name"]}" depends on it.')
    for newer in replaced_by:
        if not newer.get("restricted") and newer.get("statement"):
            lines.append(f'Now in effect: "{newer["statement"]}"')
        elif newer.get("restricted") and newer.get("summary"):
            lines.append(f"Now in effect: {newer['summary']}")
    return "\n".join(lines)


def evidence_ids(root: dict, owned: list[dict], replaced_by: list[dict]) -> list[str]:
    """The ids behind each line of the notice: owned work, then the root and replacements as stated."""

    ids = [n["id"] for n in owned]
    if not root.get("restricted") and root.get("name"):
        ids.append(root["id"])
    elif root.get("summary") and root.get("projection_id"):
        ids.append(root["projection_id"])
    for newer in replaced_by:
        if not newer.get("restricted") and newer.get("statement"):
            ids.append(newer["id"])
        elif newer.get("restricted") and newer.get("summary") and newer.get("projection_id"):
            ids.append(newer["projection_id"])
    return list(dict.fromkeys(ids))


class ImpactWatcher:
    def __init__(self, runtime: Runtime, outbox: Outbox, worker_name: str = AGENT):
        if runtime.memory is None or runtime.resolver is None:
            raise ValueError("the impact watcher needs a memory store and a resolver")
        self.runtime = runtime
        self.outbox = outbox
        self.worker = worker_name

    def drain(self, tenant: str, limit: int) -> list[WatchOutcome]:
        """Process up to ``limit`` pending events. A failing event is logged, left pending and skipped this run."""

        outcomes: list[WatchOutcome] = []
        failed: set[str] = set()
        while len(outcomes) < limit:
            event = None
            try:
                with self.outbox.claim(tenant, skip=failed) as event:
                    if event is None:
                        break
                    outcome = self.process(event)
            except Exception:
                if event is None:
                    raise
                log.exception("%s: event %s failed; it stays pending", self.worker, event.id)
                failed.add(event.id)
                outcome = WatchOutcome(event.id, event.change, error=True)
            outcomes.append(outcome)
        return outcomes

    def process(self, event: ChangeEvent) -> WatchOutcome:
        outcome = WatchOutcome(event.id, event.change)
        for person_id in affected_owners(self.runtime.open_graph(event.tenant), event.fact_id):
            self._recipient(event, person_id, outcome)
        return outcome

    def _recipient(self, event: ChangeEvent, person_id: str, outcome: WatchOutcome) -> None:
        started, request_id = time.monotonic(), uuid.uuid4().hex[:16]
        try:
            principal = self.runtime.resolver.for_person(event.tenant, person_id, Surface.SLACK_DM)
        except NotAuthorized:
            outcome.filtered += 1
            return
        result = None
        try:
            ctx = self.runtime.context(principal, agent=AGENT)
            target = event.change if event.kind in CHANGE_KINDS else event.fact_id
            result = invoke("impact_of", ctx, {"target": target}, self.runtime.cache)
            root = result.data.get("root")
            owned = owned_by(result.data.get("affected", []), person_id) if root else []
            if not owned:
                outcome.filtered += 1
                self._audit(principal, request_id, event, [], result.hidden, "filtered", started,
                            seen=len(set(result.node_ids)))
                return
            handoff = self._handoff(ctx, event, person_id, owned)
            notice = self._notice(event, person_id, root, owned, handoff)
            task_ids = []
            for task in (handoff, notice):
                if task is None:
                    continue
                written = self.runtime.memory.add_task(task, if_absent=True)
                (outcome.written if written else outcome.existing).append(task.id)
                task_ids.append(task.id)
            outcome.notified.append(person_id)
            self._audit(principal, request_id, event, result.node_ids, result.hidden, "notified", started,
                        task_ids=task_ids)
        except Exception:
            self._audit(principal, request_id, event, result.node_ids if result else [],
                        result.hidden if result else 0, "error", started)
            raise

    def _handoff(self, ctx: ToolContext, event: ChangeEvent, person_id: str, owned: list[dict]) -> Task | None:
        commitments = [n["id"] for n in owned if n.get("label") == Label.COMMITMENT]
        if not commitments:
            return None
        customers = sorted(visible_node_ids(ctx, [row[0] for row in ctx.rows(_CUSTOMERS, ids=commitments)]))
        if not customers:
            return None
        found = [row[0] for row in ctx.rows(_DEALS, ids=customers)]
        owned_deals = [n["id"] for n in owned if n.get("label") == Label.DEAL]
        deals = sorted(visible_node_ids(ctx, [*found, *owned_deals]))
        return Task(
            event.tenant, DRAFT_CUSTOMER_UPDATE, "ready", target_agent=ANSWER,
            node_ids=[*commitments, *customers, *deals],
            payload={"event_id": event.id, "change": event.change, "recipient": person_id,
                     "commitment_ids": commitments, "customer_ids": customers, "deal_ids": deals},
            id=handoff_id(event.id, person_id),
        )

    def _notice(self, event: ChangeEvent, person_id: str, root: dict, owned: list[dict],
                handoff: Task | None) -> Task:
        replaced_by = root.get("replaced_by", [])
        evidence = evidence_ids(root, owned, replaced_by)
        return Task(
            event.tenant, SLACK_DM, "ready", target_agent=SLACK_SENDER, node_ids=evidence,
            payload={"recipient": person_id, "text": compose_notice(event.kind, root, owned, replaced_by),
                     "evidence_ids": evidence, "event_id": event.id,
                     "handoff_id": handoff.id if handoff else None, "from": AGENT},
            id=notice_id(event.id, person_id),
        )

    def _audit(self, principal: Principal, request_id: str, event: ChangeEvent, node_ids: list[str], hidden: int,
               outcome: str, started: float, *, task_ids: Sequence[str] = (), seen: int = 0) -> None:
        audit = self.runtime.audit
        if audit is None:
            return
        try:
            audit.record_watch(
                request_id=request_id, principal=principal, event_id=event.id, change=event.change, tools=TOOLS,
                node_ids=list(node_ids), hidden=hidden, outcome=outcome,
                ms=int((time.monotonic() - started) * 1000), task_ids=list(task_ids), seen=seen,
            )
        except Exception:
            log.warning("watcher audit write failed (request %s)", request_id, exc_info=True)
