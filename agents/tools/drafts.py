"""draft_email and notify_owner: propose, never send.

Both write a ``tasks`` row in state ``proposed`` and nothing else. A person
confirms it (``agents.answer.actions.confirm_action``, not a tool), and only
then does anything move: an accepted note becomes a ready ``slack_dm`` task
for the Slack sender; an accepted email stays a draft for the person to send.
"""

from dataclasses import replace

from agents.gate.visible import projection_visible
from agents.memory import recall as gated
from agents.memory.store import Task
from agents.tools.base import Citation, PathEdge, RestrictedCitation, ToolContext, ToolInputError, ToolResult, tool
from agents.tools.common import item_citations, projections_for, visible_claims, visible_evidence, visible_node_ids
from shared.schema import Label, Prop, Rel

MAX_FACTS = 8
MAX_MESSAGE = 500
MAX_INTENT = 300

_FACTS = f"""
UNWIND $ids AS fid
MATCH (c {{{Prop.ID}: fid}})
RETURN c.{Prop.ID}, labels(c), c.{Prop.STATEMENT}, c.{Prop.NAME}, c.{Prop.VALUE}, c.{Prop.UNIT}, c.{Prop.AS_OF}
"""

_PROJECTIONS = f"""
UNWIND $ids AS pid
MATCH (p:{Label.PROJECTION} {{{Prop.ID}: pid}})
WHERE {projection_visible("p")}
RETURN p.{Prop.ID}, p.{Prop.SUMMARY}
"""

_PERSON = f"""
MATCH (p:{Label.PERSON})
WHERE p.{Prop.ID} = $ref OR toLower(p.{Prop.NAME}) = $lower
RETURN p.{Prop.ID}, p.{Prop.NAME}
"""


def _caller(ctx: ToolContext):
    ns = gated.namespace(ctx)
    if ns is None:
        raise ToolInputError("proposals are made for a person on a private surface")
    return ns, gated.store(ctx)


def _ids(values: list[str], what: str) -> list[str]:
    ids = list(dict.fromkeys(v for v in values if isinstance(v, str) and v))
    if not ids or len(ids) > MAX_FACTS:
        raise ToolInputError(f"give 1-{MAX_FACTS} {what}")
    return ids


def _sentence(labels: list[str], statement, name, value, unit) -> str:
    if Label.FIGURE in labels:
        amount = f"${value:,.0f}" if unit == "USD" and isinstance(value, int | float) else f"{value} {unit or ''}"
        return f"{name}: {amount.strip()}."
    text = (statement or name or "").strip()
    return text if text.endswith(".") else f"{text}."


def _footnote(c: Citation | RestrictedCitation) -> str:
    if isinstance(c, RestrictedCitation):
        return "Restricted summary (approved for you; no link)"
    when = (c.at or "")[:10]
    return ", ".join(part for part in (c.source, c.author, when, c.url) if part)


@tool(
    "draft_email",
    use_when="The user asks for an email or status update (for a customer or a colleague) built from facts "
             "you already found; returns a draft with numbered sources for them to review.",
    dont_use_when="The user wants something sent now (drafts are never sent), or you have not looked up "
                  "the facts yet (call get_decision or get_dependencies first).",
    args={
        "intent": "What the email is for, in the user's words, e.g. 'status update on SSO for Acme'.",
        "recipient": "Who it is for, e.g. 'Acme Corp' or a person's name; may be empty.",
        "fact_ids": "Decision, commitment or figure ids from earlier results to state in the email.",
    },
    mode="propose",
)
def draft_email(ctx: ToolContext, intent: str, recipient: str, fact_ids: list[str]) -> ToolResult:
    """A draft email with one sentence per fact and numbered footnotes. Stored as a proposal; never sent."""

    ns, memory = _caller(ctx)
    intent = " ".join((intent or "").split())[:MAX_INTENT]
    if not intent:
        raise ToolInputError("say what the email is for")
    facts = _ids(fact_ids, "fact ids")
    visible = visible_claims(ctx, facts)
    projected = projections_for(ctx, facts)
    rows = {row[0]: row for row in ctx.rows(_FACTS, ids=facts)}
    evidence = visible_evidence(ctx, sorted(visible))

    result = ToolResult("draft_email", {})
    footnotes: list[Citation | RestrictedCitation] = []
    lines, used = [], []
    for fact in facts:
        if fact in visible and fact in rows:
            _, labels, statement, name, value, unit, _ = rows[fact]
            cites = [*evidence.citations[fact], *projected.get(fact, [])]
            sentence = _sentence(labels, statement, name, value, unit)
            result.path += [PathEdge(fact, Rel.EVIDENCED_BY, c.item_id) for c in evidence.citations[fact]]
        elif fact in projected:
            cites = projected[fact][:1]
            sentence = cites[0].summary
        else:
            result.hidden += 1
            continue
        marks = []
        for c in cites:
            if c not in footnotes:
                footnotes.append(c)
            marks.append(f"[{footnotes.index(c) + 1}]")
        lines.append(f"- {sentence} {''.join(marks)}")
        used.append(fact)
    if not used:
        result.message = "None of those facts are visible to you, so there is nothing to draft."
        return result

    name = (recipient or "").strip()
    draft = "\n".join([
        f"To: {name or '(recipient)'}",
        f"Subject: {intent[:80]}",
        "",
        f"Hi {name.split(' ')[0] if name else 'there'},",
        "",
        "Here is where things stand:",
        *lines,
        "",
        "Best regards,",
        "",
        "Sources (for you; remove before sending):",
        *(f"[{i}] {_footnote(c)}" for i, c in enumerate(footnotes, 1)),
    ])
    item_ids = [c.item_id for c in footnotes if isinstance(c, Citation)]
    projection_ids = [c.projection_id for c in footnotes if isinstance(c, RestrictedCitation)]
    task = Task(
        ns.tenant, "proposal_email", "proposed", node_ids=[*used, *item_ids, *projection_ids],
        payload={"intent": intent, "recipient": name, "draft": draft, "facts": used},
        created_by=ns.person_id, episode_id=ctx.episode_id,
    )
    memory.add_task(task)
    result.data = {"proposal_id": task.id, "state": "proposed", "sent": False, "recipient": name,
                   "draft": draft, "facts": used}
    result.node_ids += [*used, *item_ids, *projection_ids]
    result.citations += footnotes
    return result


def resolve_person(ctx: ToolContext, ref: str) -> tuple[str, str] | None:
    rows = ctx.rows(_PERSON, ref=(ref or "").strip(), lower=(ref or "").strip().lower())
    return (rows[0][0], rows[0][1]) if len(rows) == 1 else None


def shared_citations(ctx: ToolContext, other: ToolContext, ids: list[str]) -> list[Citation | RestrictedCitation]:
    """Citations for ``ids`` that both the caller and ``other`` may see."""

    shared = [i for i in ids if i in visible_node_ids(other, ids)]
    out: list[Citation | RestrictedCitation] = []
    claim_ids = sorted(visible_claims(ctx, shared))
    if claim_ids:
        evidence = visible_evidence(ctx, claim_ids)
        cites = [c for cid in claim_ids for c in evidence.citations[cid]]
        both = visible_node_ids(other, [c.item_id for c in cites])
        out += [c for c in cites if c.item_id in both]
    out += item_citations(ctx, [i for i in shared if i not in claim_ids])
    out += [RestrictedCitation(pid, summary) for pid, summary in ctx.rows(_PROJECTIONS, ids=shared)]
    return list(dict.fromkeys(out))


@tool(
    "notify_owner",
    use_when="The user asks to tell, notify or give a heads-up to a specific colleague (often an owner from "
             "find_owner or impact_of) about something you found, with evidence ids.",
    dont_use_when="Messaging a customer (use draft_email) or anyone the user did not ask you to contact.",
    args={
        "colleague": "The colleague's person id from an earlier result, or their exact name.",
        "message": f"The note in the user's words, at most {MAX_MESSAGE} characters.",
        "evidence_ids": "Ids from earlier results that back the note (claims, items or projections).",
    },
    mode="propose",
)
def notify_owner(ctx: ToolContext, colleague: str, message: str, evidence_ids: list[str]) -> ToolResult:
    """Propose a Slack DM to a colleague. Evidence is filtered to what both of you may see; nothing is sent."""

    ns, memory = _caller(ctx)
    message = (message or "").strip()
    if not message or len(message) > MAX_MESSAGE:
        raise ToolInputError(f"message must be 1-{MAX_MESSAGE} characters")
    ids = _ids(evidence_ids, "evidence ids")
    missing = [i for i in ids if i not in visible_node_ids(ctx, ids)]
    if missing:
        raise ToolInputError(f"these ids are not available: {missing}")
    found = resolve_person(ctx, colleague)
    if found is None:
        raise ToolInputError("no single colleague by that id or name")
    person_id, person_name = found
    if person_id == ns.person_id:
        raise ToolInputError("notify someone other than yourself")
    if ctx.recipient is None:
        raise ToolInputError("notifications are not available here")
    try:
        recipient = replace(ctx, principal=ctx.recipient(person_id))
    except Exception as exc:
        raise ToolInputError("that colleague cannot receive notes from Weave") from exc

    kept = [i for i in ids if i in visible_node_ids(recipient, ids)]
    if not kept:
        raise ToolInputError(f"{person_name} cannot see any of that evidence, so nothing was proposed")
    citations = shared_citations(ctx, recipient, kept)
    task = Task(
        ns.tenant, "proposal_notify", "proposed", node_ids=kept,
        payload={"recipient": person_id, "recipient_name": person_name, "message": message,
                 "evidence_ids": kept, "withheld": len(ids) - len(kept)},
        created_by=ns.person_id, episode_id=ctx.episode_id,
    )
    memory.add_task(task)
    return ToolResult(
        "notify_owner",
        {"proposal_id": task.id, "state": "proposed", "sent": False, "recipient": person_id,
         "recipient_name": person_name, "message": message, "evidence_ids": kept,
         "withheld": len(ids) - len(kept)},
        node_ids=[person_id, *kept],
        citations=citations,
    )
