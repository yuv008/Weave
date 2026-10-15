"""confirm_action: a person accepts, edits or rejects their own proposal. Not a tool; no model can call it.

The decision is recorded on the proposal and on the episode that produced it
(feedback). An accepted note becomes a ready ``slack_dm`` task for the Slack
sender, with its evidence re-checked for both people at confirm time. An
accepted email stays a draft for the person to send.
"""

from datetime import UTC, datetime

from agents.gate.principal import Principal, Surface
from agents.memory.store import Task
from agents.runtime import Runtime
from agents.tools.base import ToolInputError
from agents.tools.common import visible_node_ids

PROPOSALS = ("proposal_email", "proposal_notify")
DECISIONS = ("accept", "edit", "reject")
MAX_TEXT = 4000
SLACK_SENDER = "slack_sender"


class ActionNotFound(LookupError):
    """No such proposal for this caller. Same error whether it exists or not."""


def confirm_action(runtime: Runtime, principal: Principal, task_id: str, decision: str,
                   text: str | None = None) -> dict:
    memory = runtime.memory
    if memory is None:
        raise ToolInputError("actions are not available here")
    task = memory.get_task(task_id) if isinstance(task_id, str) else None
    if (task is None or task.kind not in PROPOSALS or task.tenant != principal.tenant
            or not principal.user_id or task.created_by != principal.user_id or not principal.reads_private):
        raise ActionNotFound(task_id)
    if decision not in DECISIONS:
        raise ToolInputError(f"decision must be one of {list(DECISIONS)}")
    if task.state != "proposed":
        raise ToolInputError(f"this proposal is already {task.state}")
    text = (text or "").strip() or None
    if decision == "edit" and not text:
        raise ToolInputError("an edit needs the new text")
    if text and len(text) > MAX_TEXT:
        raise ToolInputError(f"text must be at most {MAX_TEXT} characters")

    outcome = {"proposal_id": task.id, "decision": decision}
    if decision == "reject":
        memory.update_task(task.id, state="rejected", payload={"decision": decision})
        outcome["state"] = "rejected"
    elif task.kind == "proposal_email":
        final = text or task.payload.get("draft", "")
        memory.update_task(task.id, state="accepted", payload={"decision": decision, "final_text": final})
        outcome.update(state="accepted", sent=False, draft=final)
    else:
        outcome.update(_queue_note(runtime, principal, task, decision, text))
    if task.episode_id:
        memory.set_feedback(task.episode_id, {
            "decision": decision, "proposal_id": task.id, "kind": task.kind,
            "at": datetime.now(UTC).isoformat(),
        })
    return outcome


def _queue_note(runtime: Runtime, principal: Principal, task: Task, decision: str, text: str | None) -> dict:
    memory = runtime.memory
    recipient_id = task.payload["recipient"]
    ids = list(task.payload.get("evidence_ids", []))
    mine = visible_node_ids(runtime.context(principal), ids)
    recipient = runtime.resolver.for_person(principal.tenant, recipient_id, Surface.SLACK_DM)
    theirs = visible_node_ids(runtime.context(recipient), ids)
    kept = [i for i in ids if i in mine and i in theirs]
    if not kept:
        memory.update_task(task.id, state="expired", payload={"decision": decision})
        raise ToolInputError("the evidence is no longer visible to both of you, so nothing was queued")
    message = text or task.payload["message"]
    dm = Task(
        task.tenant, "slack_dm", "ready", target_agent=SLACK_SENDER, node_ids=kept,
        payload={"recipient": recipient_id, "text": message, "evidence_ids": kept, "from": principal.user_id},
        created_by=principal.user_id, episode_id=task.episode_id, parent_id=task.id,
    )
    memory.add_task(dm)
    memory.update_task(task.id, state="accepted", payload={"decision": decision, "final_text": message,
                                                           "queued": dm.id})
    return {"state": "accepted", "queued_task_id": dm.id, "sent": False}
