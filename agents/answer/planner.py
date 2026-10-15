"""The rule planner: follow the role's playbook for the intent, one named step at a time.

A step reads the question, the matched subjects and earlier results, and
returns a tool call or nothing (skip). Ids always come from earlier results,
for example ``trace_decision(decision_id=<first decision found>)``.

Playbooks are versioned rows in ``playbooks`` (per tenant or global, per role
and intent); the built-in defaults below apply when none is stored.
"""

import logging
import re
from collections.abc import Callable

from agents.answer.intents import Intent
from agents.answer.loop import Finish, RunState, ToolCall
from agents.answer.subjects import first
from agents.gate.principal import Principal
from shared.schema import Label

log = logging.getLogger("weave.answer")

DEFAULT_ROLE = "default"

DEFAULT_PLAYBOOKS: dict[Intent, list[str]] = {
    Intent.LOOKUP: ["lookup", "search_if_empty"],
    Intent.PROVENANCE: ["decision", "trace", "search_if_empty"],
    Intent.OWNERSHIP: ["owners", "search_if_empty"],
    Intent.IMPACT: ["decision", "impact", "dependencies", "search_if_empty"],
    Intent.DRAFT: ["decision", "dependencies", "propose"],
}

FIGURE_WORDS = re.compile(
    r"\b(budget|arr|price|pricing|cost|costs|revenue|how much|how many|amount|spend|figures?|numbers?)\b", re.I
)
NOTIFY_WORDS = re.compile(r"\b(notify|let \w+ know|tell|ping|heads up)\b", re.I)
MAX_FACTS = 8
SEARCH_CHARS = 200

DECISION_SUBJECTS = (Label.TOPIC, Label.PROJECT, Label.DEAL)
FIGURE_SUBJECTS = (Label.DEAL, Label.PROJECT, Label.CUSTOMER, Label.TOPIC)
OWNER_SUBJECTS = (Label.PROJECT, Label.TOPIC, Label.CUSTOMER)
IMPACT_SUBJECTS = (Label.PROJECT, Label.DEAL, Label.TOPIC)
DRAFT_DEPENDENCY_SUBJECTS = (Label.DEAL, Label.PROJECT)


def role_of(principal: Principal) -> str:
    return (principal.dept or DEFAULT_ROLE).strip().lower() or DEFAULT_ROLE


def playbook_for(memory, principal: Principal, intent: Intent) -> list[str]:
    if memory is not None:
        for role in (role_of(principal), DEFAULT_ROLE):
            try:
                steps = memory.playbook(principal.tenant, role, str(intent))
            except Exception:
                log.warning("playbook lookup failed; using the default", exc_info=True)
                break
            if steps:
                return list(steps)
    return list(DEFAULT_PLAYBOOKS[intent])


def decisions(state: RunState) -> list[dict]:
    return [d for r in state.by_tool("get_decision") for d in r.data.get("decisions", [])]


def _decision(state: RunState) -> ToolCall | None:
    subject = first(state.subjects, *DECISION_SUBJECTS)
    if subject is None or state.by_tool("get_decision"):
        return None
    return ToolCall("get_decision", {"topic": subject.name}, f"decision on {subject.name}")


def _figures(state: RunState) -> ToolCall | None:
    subject = first(state.subjects, *FIGURE_SUBJECTS)
    if subject is None or state.by_tool("get_figures"):
        return None
    return ToolCall("get_figures", {"subject": subject.name}, f"figures on {subject.name}")


def _lookup(state: RunState) -> ToolCall | None:
    return _figures(state) if FIGURE_WORDS.search(state.question) else _decision(state)


def _trace(state: RunState) -> ToolCall | None:
    found = decisions(state)
    if not found or state.by_tool("trace_decision"):
        return None
    return ToolCall("trace_decision", {"decision_id": found[0]["id"]}, "provenance of the decision")


def _owners(state: RunState) -> ToolCall | None:
    subject = first(state.subjects, *OWNER_SUBJECTS)
    if subject is None:
        return None
    return ToolCall("find_owner", {"subject": subject.name}, f"owners of {subject.name}")


def _impact(state: RunState) -> ToolCall | None:
    older = [s["id"] for d in decisions(state) for s in d.get("supersedes", []) if s.get("id")]
    if not older or state.by_tool("impact_of"):
        return None
    return ToolCall("impact_of", {"target": f"{older[0]}:superseded"}, "work resting on the replaced decision")


def _dependencies(state: RunState) -> ToolCall | None:
    labels = DRAFT_DEPENDENCY_SUBJECTS if state.intent is Intent.DRAFT else IMPACT_SUBJECTS
    subject = first(state.subjects, *labels)
    if subject is None:
        return None
    direction = "downstream" if state.intent is Intent.DRAFT else "upstream"
    return ToolCall("get_dependencies", {"entity": subject.name, "depth": 2, "direction": direction},
                    f"{direction} dependencies of {subject.name}")


def _search_if_empty(state: RunState) -> ToolCall | None:
    if state.has_facts():
        return None
    return ToolCall("search_context", {"query": state.question[:SEARCH_CHARS]}, "no structured facts found")


def fact_ids(state: RunState) -> list[str]:
    """Claims a draft may state: seed facts first, then decisions and commitments found, never figures."""

    ids = [*state.seed_facts, *(d["id"] for d in decisions(state))]
    for r in state.by_tool("get_dependencies"):
        ids += [n["id"] for n in r.data.get("nodes", []) if n.get("label") in (Label.COMMITMENT, Label.DECISION)]
    return list(dict.fromkeys(ids))[:MAX_FACTS]


def _propose(state: RunState) -> ToolCall | None:
    facts = fact_ids(state)
    if not facts:
        return None
    person = next((m for m in state.subjects
                   if m.label == Label.PERSON and m.id != state.principal.user_id), None)
    if person is not None and NOTIFY_WORDS.search(state.question):
        projections = [c.projection_id for r in state.results for c in r.citations if hasattr(c, "projection_id")]
        evidence = list(dict.fromkeys([*facts, *projections]))[:MAX_FACTS]
        return ToolCall("notify_owner", {"colleague": person.id, "message": state.question, "evidence_ids": evidence},
                        f"propose a note to {person.name}")
    recipient = first(state.subjects, Label.CUSTOMER, Label.PERSON, Label.DEAL)
    return ToolCall("draft_email", {"intent": state.question, "recipient": recipient.name if recipient else "",
                                    "fact_ids": facts}, "draft from the facts found")


STEPS: dict[str, Callable[[RunState], ToolCall | None]] = {
    "lookup": _lookup,
    "decision": _decision,
    "figures": _figures,
    "trace": _trace,
    "owners": _owners,
    "impact": _impact,
    "dependencies": _dependencies,
    "search_if_empty": _search_if_empty,
    "propose": _propose,
}


class RulePlanner:
    """Walks ``state.playbook``; unknown step names are skipped."""

    def next_step(self, state: RunState) -> ToolCall | Finish:
        while state.cursor < len(state.playbook):
            name = state.playbook[state.cursor]
            state.cursor += 1
            step = STEPS.get(name)
            if step is None:
                log.warning("unknown playbook step %r", name)
                continue
            call = step(state)
            if call is not None:
                return call
        return Finish("playbook done")
