"""get_decision and trace_decision (flow 1)."""

from datetime import UTC, datetime

from agents.gate.visible import item_visible, keep_visible_claims
from agents.tools.base import PathEdge, ToolContext, ToolInputError, ToolResult, tool
from agents.tools.common import (
    hidden_evidence,
    projections_for,
    supersession,
    visible_claims,
    visible_evidence,
)
from agents.tools.subjects import resolve_subject
from shared.schema import Label, Prop, Rel, Status
from shared.timefmt import parse_ts, to_ts

NOT_FOUND = "No decision on that is visible to you. Try search_context."

_TIME_ACTIVE = f"d.{Prop.STATUS} = '{Status.ACTIVE}'"
_TIME_AS_OF = (
    f"d.{Prop.STATUS} <> '{Status.RETRACTED}' AND d.{Prop.VALID_FROM} <= $as_of"
    f" AND (d.{Prop.VALID_TO} IS NULL OR d.{Prop.VALID_TO} > $as_of)"
)


def _candidates(by_subject: bool, as_of: bool) -> str:
    match = (
        f"MATCH (d:{Label.DECISION})-[:{Rel.ABOUT}]->(s {{{Prop.ID}: $subject}})"
        if by_subject
        else f"MATCH (d:{Label.DECISION} {{{Prop.ID}: $decision}})"
    )
    when = _TIME_AS_OF if as_of else _TIME_ACTIVE
    return f"{match} WHERE {when if by_subject else 'true'} RETURN d.{Prop.ID}"


_DECISIONS = f"""
UNWIND $ids AS did
MATCH (d:{Label.DECISION} {{{Prop.ID}: did}})
RETURN d.{Prop.ID}, d.{Prop.STATEMENT}, d.{Prop.STATUS}, d.{Prop.VALID_FROM}, d.{Prop.VALID_TO},
       d.{Prop.CONFIDENCE}, d.{Prop.LAST_VERIFIED_AT}
"""

def _as_of(value: str | None) -> str | None:
    if value is None:
        return None
    try:
        if len(value) == 10:
            moment = datetime.fromisoformat(value).replace(hour=23, minute=59, second=59, tzinfo=UTC)
        else:
            moment = parse_ts(value)
    except ValueError as exc:
        raise ToolInputError("as_of must be a date (YYYY-MM-DD) or a timestamp") from exc
    return to_ts(moment)


def _card(row, ctx: ToolContext) -> dict:
    decision_id, statement, status, valid_from, valid_to, confidence, verified = row
    return {
        "id": decision_id,
        "statement": statement,
        "status": status,
        "valid_from": valid_from,
        "valid_to": valid_to,
        "confidence": confidence,
        "likely": ctx.likely(confidence),
        "last_verified_at": verified,
        "restricted": False,
    }


def _cards(ctx: ToolContext, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    return {row[0]: _card(row, ctx) for row in ctx.rows(_DECISIONS, ids=ids)}


def _related(ctx: ToolContext, decision_id: str, result: ToolResult, *, older: bool) -> list[dict]:
    """Older or newer decisions in the chain: shown if visible, as a summary if projected, else counted."""

    entries, hidden = supersession(ctx, Label.DECISION, decision_id, older=older)
    result.hidden += hidden
    cards = _cards(ctx, [other for other, restricted in entries if restricted is None])
    out = []
    for other, restricted in entries:
        if restricted is None:
            card = cards[other]
            out.append({k: card[k] for k in ("id", "statement", "valid_from", "valid_to")})
        else:
            out.append({"id": other, "summary": restricted.summary, "restricted": True})
            result.citations.append(restricted)
        result.node_ids.append(other)
    return out


@tool(
    "get_decision",
    use_when="You need the decision the team made on a topic, API, library or repo, with who decided, when, and the evidence.",
    dont_use_when="You need numbers (use get_figures), owners (use find_owner) or free-text search (use search_context).",
    args={
        "topic": "What the decision is about: a topic, project, API or library name or alias, e.g. 'SSO'.",
        "decision_id": "A decision id from an earlier result; use instead of topic to fetch one decision.",
        "repo": "The repository you are working in, e.g. 'demo-org/platform'; used when no topic is given.",
        "as_of": "Optional date (YYYY-MM-DD) or timestamp: return what was in force then instead of now.",
    },
)
def get_decision(
    ctx: ToolContext,
    topic: str | None = None,
    decision_id: str | None = None,
    repo: str | None = None,
    as_of: str | None = None,
) -> ToolResult:
    """Active decision on a topic (or the one with ``decision_id``), its supersession chain and visible evidence.

    ``repo`` is the caller's repository; it picks the subject when no topic is
    given. ``as_of`` returns what was in force at that date instead.
    """

    result = ToolResult("get_decision", {"decisions": [], "conflict": False})
    as_of_ts = _as_of(as_of)
    if decision_id:
        ids = [row[0] for row in ctx.rows(_candidates(False, False), decision=decision_id)]
        subject = None
    else:
        subject = resolve_subject(ctx, topic) if topic else None
        if subject is None and repo:
            subject = resolve_subject(ctx, repo, (Label.PROJECT,))
        if subject is None:
            if not topic and not repo:
                raise ToolInputError("give a topic, a repo or a decision_id")
            result.message = NOT_FOUND
            return result
        ids = [row[0] for row in ctx.rows(_candidates(True, as_of_ts is not None), subject=subject.id, as_of=as_of_ts)]
        result.data["subject"] = {"id": subject.id, "name": subject.name, "label": subject.label}

    visible = visible_claims(ctx, ids)
    result.hidden += hidden_evidence(ctx, sorted(visible))
    if not visible:
        projected = projections_for(ctx, ids)
        for decision, restricted in projected.items():
            result.data["decisions"].append({"id": decision, "summary": restricted[0].summary, "restricted": True})
            result.citations.extend(restricted)
            result.node_ids.append(decision)
        result.hidden += len(set(ids) - set(projected))
        if not projected:
            result.message = NOT_FOUND
        return result

    result.hidden += len(set(ids) - visible)
    cards = _cards(ctx, sorted(visible))
    ordered = sorted(cards.values(), key=lambda c: c["valid_from"] or "", reverse=True)
    evidence = visible_evidence(ctx, [c["id"] for c in ordered])
    for card in ordered:
        decision = card["id"]
        card["evidence"] = evidence.items(decision)
        card["supersedes"] = _related(ctx, decision, result, older=True)
        card["replaces"] = next((c for c in card["supersedes"] if not c.get("restricted")), None)
        if decision_id:
            card["superseded_by"] = _related(ctx, decision, result, older=False)
        result.node_ids += [decision, *card["evidence"]]
        result.citations += evidence.citations[decision]
        result.path += [PathEdge(decision, Rel.EVIDENCED_BY, item) for item in card["evidence"]]
        if subject:
            result.path.append(PathEdge(decision, Rel.ABOUT, subject.id))
        for older in card["supersedes"]:
            if not older.get("restricted"):
                result.path.append(PathEdge(decision, Rel.SUPERSEDES, older["id"]))
        result.data["decisions"].append(card)
    result.data["conflict"] = decision_id is None and len(ordered) > 1
    return result


_TRACE_EVIDENCE = f"""
MATCH (d:{Label.DECISION} {{{Prop.ID}: $id}})-[e:{Rel.EVIDENCED_BY}]->(i:{Label.ITEM})
WITH d, e, i, {item_visible("i")} AS ok
OPTIONAL MATCH (i)-[:{Rel.AUTHORED_BY}]->(p:{Label.PERSON})
OPTIONAL MATCH (i)-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE})
RETURN ok, i.{Prop.ID}, e.{Prop.QUOTE}, i.{Prop.OCCURRED_AT}, i.{Prop.SOURCE_URL},
       p.{Prop.ID}, p.{Prop.NAME}, s.{Prop.ID}, s.{Prop.NAME}
ORDER BY i.{Prop.OCCURRED_AT}
"""

_THREAD_OUT = f"""
UNWIND $ids AS iid
MATCH (i:{Label.ITEM} {{{Prop.ID}: iid}})-[r:{Rel.REPLIES_TO}|{Rel.LINKS_TO}]->(t:{Label.ITEM})
RETURN iid, type(r), t.{Prop.ID}, {item_visible("t")}, t.{Prop.EXCERPT}, t.{Prop.SOURCE_URL}, t.{Prop.OCCURRED_AT}
"""

_THREAD_IN = f"""
UNWIND $ids AS iid
MATCH (t:{Label.ITEM})-[r:{Rel.REPLIES_TO}|{Rel.LINKS_TO}]->(i:{Label.ITEM} {{{Prop.ID}: iid}})
RETURN iid, type(r), t.{Prop.ID}, {item_visible("t")}, t.{Prop.EXCERPT}, t.{Prop.SOURCE_URL}, t.{Prop.OCCURRED_AT}
"""

_VISIBLE_DECISION = f"""
MATCH (d:{Label.DECISION} {{{Prop.ID}: $id}})
{keep_visible_claims("d")}
RETURN d.{Prop.ID}
"""


@tool(
    "trace_decision",
    use_when="You have a decision id and need its provenance: every evidence item, its author, source and thread.",
    dont_use_when="You only need the current decision on a topic (use get_decision).",
    args={"decision_id": "The decision id, as returned by get_decision."},
)
def trace_decision(ctx: ToolContext, decision_id: str) -> ToolResult:
    """Decision to evidence items to authors and threads. Hidden hops are counted, never named."""

    result = ToolResult("trace_decision", {"decision": None, "evidence": []})
    if not ctx.rows(_VISIBLE_DECISION, id=decision_id):
        projected = projections_for(ctx, [decision_id]).get(decision_id)
        if projected:
            result.data["decision"] = {"id": decision_id, "summary": projected[0].summary, "restricted": True}
            result.citations.extend(projected)
            result.node_ids.append(decision_id)
        else:
            result.message = "No decision with that id is visible to you."
        return result

    result.data["decision"] = _cards(ctx, [decision_id])[decision_id]
    result.node_ids.append(decision_id)
    evidence = {}
    for ok, item, quote, at, url, person, author, source_id, source in ctx.rows(_TRACE_EVIDENCE, id=decision_id):
        if not ok:
            result.hidden += 1
            continue
        evidence[item] = {
            "item_id": item, "quote": quote, "at": at, "url": url,
            "author": author, "author_id": person, "source": source, "thread": [],
        }
        result.node_ids.append(item)
        result.path.append(PathEdge(decision_id, Rel.EVIDENCED_BY, item))
        if person:
            result.node_ids.append(person)
            result.path.append(PathEdge(item, Rel.AUTHORED_BY, person))
        if source_id:
            result.path.append(PathEdge(item, Rel.IN_SOURCE, source_id))
    citations = visible_evidence(ctx, [decision_id]).citations[decision_id]
    result.citations += citations

    ids = list(evidence)
    for query, outgoing in ((_THREAD_OUT, True), (_THREAD_IN, False)):
        for item, rel, other, ok, excerpt, url, at in ctx.rows(query, ids=ids) if ids else []:
            if not ok:
                result.hidden += 1
                continue
            evidence[item]["thread"].append(
                {"item_id": other, "rel": rel, "direction": "out" if outgoing else "in",
                 "excerpt": excerpt, "url": url, "at": at}
            )
            result.node_ids.append(other)
            result.path.append(PathEdge(item, rel, other) if outgoing else PathEdge(other, rel, item))
    result.data["evidence"] = list(evidence.values())
    return result
