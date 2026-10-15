"""Gated lookups several tools share. Every query goes through ``agents.gate.visible``."""

from dataclasses import dataclass, field

from agents.gate.visible import item_visible, keep_visible_claims, projection_visible
from agents.tools.base import Citation, RestrictedCitation, ToolContext, citation
from shared.schema import Label, Prop, Rel

_CITATIONS = f"""
UNWIND $ids AS cid
MATCH (c {{{Prop.ID}: cid}})-[e:{Rel.EVIDENCED_BY}]->(i:{Label.ITEM})
WHERE {item_visible("i")}
OPTIONAL MATCH (i)-[:{Rel.AUTHORED_BY}]->(p:{Label.PERSON})
OPTIONAL MATCH (i)-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE})
RETURN cid, i.{Prop.ID}, e.{Prop.QUOTE}, p.{Prop.NAME}, i.{Prop.OCCURRED_AT}, s.{Prop.NAME}, i.{Prop.SOURCE_URL}, p.{Prop.ID}
ORDER BY i.{Prop.OCCURRED_AT}
"""

_HIDDEN_EVIDENCE = f"""
UNWIND $ids AS cid
MATCH (c {{{Prop.ID}: cid}})-[:{Rel.EVIDENCED_BY}]->(i:{Label.ITEM})
WHERE NOT {item_visible("i")}
RETURN count(i)
"""

_VISIBLE_CLAIMS = f"""
UNWIND $ids AS cid
MATCH (d {{{Prop.ID}: cid}})
{keep_visible_claims("d")}
RETURN d.{Prop.ID}
"""

_PROJECTIONS = f"""
UNWIND $ids AS cid
MATCH (p:{Label.PROJECTION})-[:{Rel.SUMMARIZES}]->(c {{{Prop.ID}: cid}})
WHERE {projection_visible("p")}
RETURN cid, p.{Prop.ID}, p.{Prop.SUMMARY}
"""


@dataclass
class Evidence:
    citations: dict[str, list[Citation]]
    """Visible evidence per claim, oldest first."""
    authors: dict[str, str] = field(default_factory=dict)
    """Visible evidence item id -> author person id."""

    def items(self, claim_id: str) -> list[str]:
        return [c.item_id for c in self.citations.get(claim_id, [])]


CHAIN_DEPTH = 6


def supersession(
    ctx: ToolContext, label: str, claim_id: str, *, older: bool = True
) -> tuple[list[tuple[str, RestrictedCitation | None]], int]:
    """Claims replaced by (``older``) or replacing ``claim_id``, nearest first.

    Returns visible ids paired with ``None``, projected ids paired with their
    summary, and the number of hidden ones, which are never named.
    """

    pattern = (
        f"(c:{label} {{{Prop.ID}: $id}})-[:{Rel.SUPERSEDES}*1..{CHAIN_DEPTH}]->(o:{label})"
        if older
        else f"(o:{label})-[:{Rel.SUPERSEDES}*1..{CHAIN_DEPTH}]->(c:{label} {{{Prop.ID}: $id}})"
    )
    chain = [row[0] for row in ctx.rows(f"MATCH path = {pattern} RETURN o.{Prop.ID}, length(path) ORDER BY length(path)", id=claim_id)]
    seen = visible_claims(ctx, chain)
    projected = projections_for(ctx, [c for c in chain if c not in seen])
    entries, hidden = [], 0
    for other in chain:
        if other in seen:
            entries.append((other, None))
        elif other in projected:
            entries.append((other, projected[other][0]))
        else:
            hidden += 1
    return entries, hidden


def visible_evidence(ctx: ToolContext, claim_ids: list[str]) -> Evidence:
    evidence = Evidence({cid: [] for cid in claim_ids})
    if not claim_ids:
        return evidence
    for cid, item_id, quote, author, at, source, url, person_id in ctx.rows(_CITATIONS, ids=list(claim_ids)):
        evidence.citations[cid].append(
            citation({"id": item_id, "quote": quote, "author": author, "at": at, "source": source, "url": url})
        )
        if person_id:
            evidence.authors[item_id] = person_id
    return evidence


_ITEMS = f"""
UNWIND $ids AS iid
MATCH (i:{Label.ITEM} {{{Prop.ID}: iid}})
WHERE {item_visible("i")}
OPTIONAL MATCH (i)-[:{Rel.AUTHORED_BY}]->(p:{Label.PERSON})
OPTIONAL MATCH (i)-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE})
RETURN i.{Prop.ID}, i.{Prop.EXCERPT}, p.{Prop.NAME}, i.{Prop.OCCURRED_AT}, s.{Prop.NAME}, i.{Prop.SOURCE_URL}
"""


def item_citations(ctx: ToolContext, item_ids: list[str]) -> list[Citation]:
    """Citations for items cited directly (edge evidence, search hits). Invisible ids are dropped."""

    if not item_ids:
        return []
    return [
        citation({"id": item, "quote": excerpt, "author": author, "at": at, "source": source, "url": url})
        for item, excerpt, author, at, source, url in ctx.rows(_ITEMS, ids=sorted(set(item_ids)))
    ]


def hidden_evidence(ctx: ToolContext, claim_ids: list[str]) -> int:
    if not claim_ids:
        return 0
    return ctx.rows(_HIDDEN_EVIDENCE, ids=list(claim_ids))[0][0]


def visible_claims(ctx: ToolContext, claim_ids: list[str]) -> set[str]:
    if not claim_ids:
        return set()
    return {row[0] for row in ctx.rows(_VISIBLE_CLAIMS, ids=list(claim_ids))}


_LABELS = f"""
UNWIND $ids AS nid
MATCH (n {{{Prop.ID}: nid}})
RETURN nid, labels(n), coalesce(n.{Prop.PROVISIONAL}, false)
"""

_VISIBLE_ITEMS = f"""
UNWIND $ids AS nid
MATCH (i {{{Prop.ID}: nid}})
WHERE {item_visible("i")}
RETURN i.{Prop.ID}
"""

_VISIBLE_PROJECTIONS = f"""
UNWIND $ids AS nid
MATCH (p:{Label.PROJECTION} {{{Prop.ID}: nid}})
WHERE {projection_visible("p")}
RETURN p.{Prop.ID}
"""

CLAIM_LABELS = (Label.DECISION, Label.COMMITMENT, Label.FIGURE)


def visible_node_ids(ctx: ToolContext, node_ids: list[str]) -> set[str]:
    """The ids the caller may see now. Unknown ids are not visible.

    Items and sources use the source rule, claims need visible evidence,
    projections must be approved for the caller, and other nodes (people,
    teams, topics, projects, customers, deals) are visible unless provisional
    and hidden by config.
    """

    ids = sorted({i for i in node_ids if isinstance(i, str) and i})
    if not ids:
        return set()
    items, claims, projections, visible = [], [], [], set()
    for nid, labels, provisional in ctx.rows(_LABELS, ids=ids):
        if Label.ITEM in labels or Label.SOURCE in labels:
            items.append(nid)
        elif any(label in labels for label in CLAIM_LABELS):
            claims.append(nid)
        elif Label.PROJECTION in labels:
            projections.append(nid)
        elif not (provisional and ctx.cfg.hide_provisional):
            visible.add(nid)
    if items:
        visible |= {row[0] for row in ctx.rows(_VISIBLE_ITEMS, ids=items)}
    visible |= visible_claims(ctx, claims)
    if projections:
        visible |= {row[0] for row in ctx.rows(_VISIBLE_PROJECTIONS, ids=projections)}
    return visible


def projections_for(ctx: ToolContext, claim_ids: list[str]) -> dict[str, list[RestrictedCitation]]:
    """Approved projections the caller may see, per claim."""

    out: dict[str, list[RestrictedCitation]] = {}
    if not claim_ids:
        return out
    for cid, projection_id, summary in ctx.rows(_PROJECTIONS, ids=list(claim_ids)):
        out.setdefault(cid, []).append(RestrictedCitation(projection_id, summary))
    return out
