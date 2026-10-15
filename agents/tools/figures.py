"""get_figures: the latest numbers about a subject, with as-of date, source, history and a stale badge."""

from agents.tools.base import PathEdge, ToolContext, ToolInputError, ToolResult, tool
from agents.tools.common import hidden_evidence, projections_for, supersession, visible_claims, visible_evidence
from agents.tools.subjects import resolve_subject
from shared.schema import Label, Prop, Rel, Status

_ACTIVE = f"""
MATCH (f:{Label.FIGURE})-[:{Rel.ABOUT}]->(s {{{Prop.ID}: $subject}})
WHERE f.{Prop.STATUS} = '{Status.ACTIVE}'
  AND ($name IS NULL OR toLower(f.{Prop.NAME}) CONTAINS $name)
RETURN f.{Prop.ID}
"""

_FIGURES = f"""
UNWIND $ids AS fid
MATCH (f:{Label.FIGURE} {{{Prop.ID}: fid}})
RETURN f.{Prop.ID}, f.{Prop.NAME}, f.{Prop.VALUE}, f.{Prop.UNIT}, f.{Prop.AS_OF}, f.{Prop.VALID_TO},
       f.{Prop.CONFIDENCE}
"""


def _cards(ctx: ToolContext, ids: list[str]) -> dict[str, dict]:
    if not ids:
        return {}
    cards = {}
    for figure, name, value, unit, as_of, valid_to, confidence in ctx.rows(_FIGURES, ids=ids):
        age = ctx.age_days(as_of)
        cards[figure] = {
            "id": figure, "name": name, "value": value, "unit": unit, "as_of": as_of, "valid_to": valid_to,
            "confidence": confidence, "likely": ctx.likely(confidence),
            "stale": age is not None and age > ctx.cfg.figure_stale_days,
        }
    return cards


@tool(
    "get_figures",
    use_when="You need a number about a project, deal or topic: budget, ARR, price, date-bound quantity.",
    dont_use_when="You need a decision (use get_decision) or people (use find_owner).",
    args={
        "subject": "The project, deal or topic the number is about, by name, alias or id.",
        "name": "Optional figure name to narrow the result, e.g. 'Q4 platform budget'.",
    },
)
def get_figures(ctx: ToolContext, subject: str, name: str | None = None) -> ToolResult:
    """Latest active figures the caller may see, each with as-of date, sources, history and a stale flag."""

    if not subject:
        raise ToolInputError("give a project, deal or topic")
    result = ToolResult("get_figures", {"figures": []})
    found = resolve_subject(ctx, subject, (Label.PROJECT, Label.DEAL, Label.TOPIC, Label.CUSTOMER))
    if found is None:
        result.message = "No figures on that are visible to you."
        return result
    result.data["subject"] = {"id": found.id, "name": found.name, "label": found.label}
    ids = [row[0] for row in ctx.rows(_ACTIVE, subject=found.id, name=name.lower() if name else None)]
    visible = visible_claims(ctx, ids)
    result.hidden += hidden_evidence(ctx, sorted(visible))

    if not visible:
        projected = projections_for(ctx, ids)
        for figure, restricted in projected.items():
            result.data["figures"].append({"id": figure, "summary": restricted[0].summary, "restricted": True,
                                           "projection_id": restricted[0].projection_id})
            result.citations.extend(restricted)
            result.node_ids.append(figure)
        result.hidden += len(set(ids) - set(projected))
        if not projected:
            result.message = "No figures on that are visible to you."
        return result

    result.hidden += len(set(ids) - visible)
    cards = _cards(ctx, sorted(visible))
    evidence = visible_evidence(ctx, sorted(visible))
    for card in sorted(cards.values(), key=lambda c: c["as_of"] or "", reverse=True):
        figure = card["id"]
        citations = evidence.citations[figure]
        card["evidence"] = [c.item_id for c in citations]
        card["sources"] = sorted({c.source for c in citations if c.source})
        entries, hidden = supersession(ctx, Label.FIGURE, figure)
        result.hidden += hidden
        older = _cards(ctx, [other for other, restricted in entries if restricted is None])
        card["history"] = []
        for other, restricted in entries:
            if restricted is None:
                card["history"].append({k: older[other][k] for k in ("id", "value", "unit", "as_of", "valid_to")})
                result.path.append(PathEdge(figure, Rel.SUPERSEDES, other))
            else:
                card["history"].append({"id": other, "summary": restricted.summary, "restricted": True,
                                        "projection_id": restricted.projection_id})
                result.citations.append(restricted)
            result.node_ids.append(other)
        result.data["figures"].append(card)
        result.node_ids += [figure, *card["evidence"]]
        result.citations += citations
        result.path.append(PathEdge(figure, Rel.ABOUT, found.id))
        result.path += [PathEdge(figure, Rel.EVIDENCED_BY, item) for item in card["evidence"]]
    return result
