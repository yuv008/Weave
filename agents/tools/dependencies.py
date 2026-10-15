"""get_dependencies, impact_of and dept_rollup: the Sales-to-Engineering bridge.

Walks expand one hop per query so every hop is gated: an edge counts only
when the caller can see at least one of its ``evidence_ids`` items and every
claim at either end. ``impact_of`` exempts its root, the changed decision,
from the claim check: the root shows as restricted while the caller still
learns which of their own visible work rests on it (flow 3).
"""

from collections import Counter

from agents.gate.visible import keep_visible_edges
from agents.graph import ReadOnlyGraph
from agents.tools.base import PathEdge, ToolContext, ToolInputError, ToolResult, tool
from agents.tools.common import item_citations, projections_for, supersession, visible_claims
from agents.tools.subjects import resolve_subject
from shared.schema import CLAIM_LABELS, CONTRACT_LABELS, Label, Progress, Prop, Rel, Status

MAX_DEPTH = 3
CHANGE_KINDS = ("superseded", "retracted", "contradicts")
DIRECTIONS = {"downstream": (True,), "upstream": (False,), "both": (True, False)}
WALK_RELS = (Rel.NEEDS, Rel.DEPENDS_ON, Rel.BLOCKED_BY)
IMPACT_RELS = (Rel.DEPENDS_ON, Rel.NEEDS)
OPEN_PROGRESS = [Progress.OPEN, Progress.AT_RISK]

_LABELS = f"UNWIND $ids AS nid MATCH (n {{{Prop.ID}: nid}}) RETURN n.{Prop.ID}, labels(n)"

_NODES = f"""
UNWIND $ids AS nid
MATCH (n {{{Prop.ID}: nid}})
OPTIONAL MATCH (o:{Label.PERSON})-[:{Rel.OWNS}]->(n)
OPTIONAL MATCH (n)-[:{Rel.MADE_BY}]->(m:{Label.PERSON})
RETURN n.{Prop.ID}, labels(n), coalesce(n.{Prop.NAME}, n.{Prop.STATEMENT}), n.{Prop.STATUS}, n.{Prop.PROGRESS},
       collect(DISTINCT {{id: o.{Prop.ID}, name: o.{Prop.NAME}, dept: o.{Prop.DEPT}}}),
       collect(DISTINCT m.{Prop.DEPT}), n.{Prop.DUE}
"""


def _hop(rels: tuple[str, ...], outgoing: bool, gated: bool) -> str:
    rel = "|".join(rels)
    pattern = (
        f"(a {{{Prop.ID}: nid}})-[r:{rel}]->(b)" if outgoing else f"(b)-[r:{rel}]->(a {{{Prop.ID}: nid}})"
    )
    if not gated:
        return f"UNWIND $ids AS nid MATCH {pattern} RETURN count(r)"
    return f"""
UNWIND $ids AS nid
MATCH {pattern}
{keep_visible_edges("r", carry=("a", "b"))}
RETURN a.{Prop.ID}, type(r), b.{Prop.ID}, [gate_x IN gate_edge_evidence | gate_x.{Prop.ID}]
"""


def _label(labels: list[str]) -> str:
    return next((label for label in CONTRACT_LABELS if label in labels), labels[0] if labels else "")


def _labels(ctx: ToolContext, ids) -> dict[str, str]:
    ids = sorted(set(ids))
    return {row[0]: _label(row[1]) for row in ctx.rows(_LABELS, ids=ids)} if ids else {}


def _nodes(ctx: ToolContext, ids) -> dict[str, dict]:
    ids = sorted(set(ids))
    out = {}
    for node, labels, name, status, progress, owners, maker_depts, due in ctx.rows(_NODES, ids=ids) if ids else []:
        owners = [o for o in owners if o.get("id")]
        depts = [o["dept"] for o in owners if o.get("dept")] + [d for d in maker_depts if d]
        out[node] = {
            "id": node, "label": _label(labels), "name": name, "status": status, "progress": progress,
            "due": due, "owners": owners, "dept": depts[0] if depts else None,
        }
    return out


def _walk(ctx: ToolContext, root: str, depth: int, directions: tuple[bool, ...], rels: tuple[str, ...],
          exempt: frozenset[str] = frozenset()):
    """Gated breadth-first walk. Returns edges ``(src, rel, dst, evidence)``, reached nodes and the hidden count."""

    frontier, seen, edges, hidden = {root}, {root}, [], 0
    for _ in range(depth):
        if not frontier:
            break
        found = set()
        ids = sorted(frontier)
        for outgoing in directions:
            rows = ctx.rows(_hop(rels, outgoing, gated=True), ids=ids)
            hidden += ctx.rows(_hop(rels, outgoing, gated=False), ids=ids)[0][0] - len(rows)
            labels = _labels(ctx, [row[2] for row in rows])
            claims = [o for o, label in labels.items() if label in CLAIM_LABELS and o not in exempt]
            ok = visible_claims(ctx, claims)
            for anchor, rel, other, evidence in rows:
                if other in claims and other not in ok:
                    hidden += 1
                    continue
                edge = (anchor, rel, other, evidence) if outgoing else (other, rel, anchor, evidence)
                if edge not in edges:
                    edges.append(edge)
                if other not in seen:
                    found.add(other)
        seen |= found
        frontier = found
    return edges, seen, hidden


def _edge_rows(edges, nodes: dict[str, dict]) -> list[dict]:
    rows = []
    for src, rel, dst, evidence in edges:
        src_dept, dst_dept = nodes.get(src, {}).get("dept"), nodes.get(dst, {}).get("dept")
        rows.append({
            "src": src, "rel": rel, "dst": dst, "evidence": evidence,
            "src_dept": src_dept, "dst_dept": dst_dept,
            "cross_department": bool(src_dept and dst_dept and src_dept != dst_dept),
        })
    return rows


def _root(ctx: ToolContext, ref: str) -> tuple[str | None, str | None]:
    """An exact node id (claims must be visible), else a subject by name or alias."""

    labels = _labels(ctx, [ref])
    if ref in labels:
        label = labels[ref]
        if label in CLAIM_LABELS and ref not in visible_claims(ctx, [ref]):
            return None, None
        return ref, label
    subject = resolve_subject(ctx, ref)
    return (subject.id, subject.label) if subject else (None, None)


@tool(
    "get_dependencies",
    use_when="You need what a deal, project, commitment or decision depends on, or what depends on it, across teams.",
    dont_use_when="You need the effect of a specific change (use impact_of) or department totals (use dept_rollup).",
    args={
        "entity": "A deal, project, commitment or decision, by name, alias or id.",
        "depth": f"How many hops to walk, 1 to {MAX_DEPTH}.",
        "direction": "'downstream' for what it depends on, 'upstream' for what depends on it, or 'both'.",
    },
    choices={"direction": tuple(sorted(DIRECTIONS))},
)
def get_dependencies(ctx: ToolContext, entity: str, depth: int = 1, direction: str = "both") -> ToolResult:
    """Neighbours over NEEDS, DEPENDS_ON and BLOCKED_BY, each node labelled by its owner's department."""

    if direction not in DIRECTIONS:
        raise ToolInputError(f"direction must be one of {sorted(DIRECTIONS)}")
    if not 1 <= int(depth) <= MAX_DEPTH:
        raise ToolInputError(f"depth must be between 1 and {MAX_DEPTH}")
    result = ToolResult("get_dependencies", {"root": None, "nodes": [], "edges": []})
    root, _ = _root(ctx, entity) if entity else (None, None)
    if root is None:
        result.message = "Nothing by that name is visible to you."
        return result

    edges, reached, hidden = _walk(ctx, root, int(depth), DIRECTIONS[direction], WALK_RELS)
    result.hidden += hidden
    nodes = _nodes(ctx, reached)
    result.data["root"] = nodes[root]
    result.data["nodes"] = [nodes[n] for n in sorted(reached - {root})]
    result.data["edges"] = _edge_rows(edges, nodes)
    result.node_ids += [root, *sorted(reached - {root})]
    result.path += [PathEdge(src, rel, dst) for src, rel, dst, _ in edges]
    result.citations += item_citations(ctx, [item for *_, evidence in edges for item in evidence])
    return result


@tool(
    "impact_of",
    use_when="A decision changed or was reversed and you need the commitments, deals and projects resting on it, with owners.",
    dont_use_when="You need plain neighbours of an entity (use get_dependencies).",
    args={
        "target": (
            "A decision id, or a change id '{fact_id}:{kind}' where kind is one of "
            + ", ".join(CHANGE_KINDS) + "."
        ),
    },
)
def impact_of(ctx: ToolContext, target: str) -> ToolResult:
    """Affected work up to three hops from a decision or change id (``{fact_id}:{kind}``), filtered for the caller."""

    if not target:
        raise ToolInputError("give a decision id or a change id")
    fact = target
    head, _, kind = target.rpartition(":")
    if kind in CHANGE_KINDS:
        fact = head
    result = ToolResult("impact_of", {"root": None, "affected": [], "edges": [], "notify": []})
    labels = _labels(ctx, [fact])
    if fact not in labels:
        result.message = "Nothing by that id is visible to you."
        return result

    edges, reached, hidden = _walk(ctx, fact, MAX_DEPTH, (False,), IMPACT_RELS, exempt=frozenset({fact}))
    result.hidden += hidden
    affected = reached - {fact}
    nodes = _nodes(ctx, affected | {fact})
    visible_root = labels[fact] not in CLAIM_LABELS or fact in visible_claims(ctx, [fact])
    projected = projections_for(ctx, [fact]).get(fact) if not visible_root else None
    if not affected and not visible_root and not projected:
        result.message = "Nothing by that id is visible to you."
        return result

    if not visible_root:
        nodes[fact] = {"id": fact, "label": labels[fact], "dept": None, "owners": []}
    if visible_root:
        root = nodes[fact]
    elif projected:
        root = {"id": fact, "label": labels[fact], "summary": projected[0].summary, "restricted": True,
                "projection_id": projected[0].projection_id}
        result.citations += projected
    else:
        root = {"id": fact, "label": labels[fact], "restricted": True}
    if labels[fact] in (Label.DECISION, Label.FIGURE, Label.COMMITMENT):
        entries, newer_hidden = supersession(ctx, labels[fact], fact, older=False)
        result.hidden += newer_hidden
        newer = _nodes(ctx, [other for other, restricted in entries if restricted is None])
        root["replaced_by"] = [
            {"id": o, "statement": newer[o]["name"]} if r is None
            else {"id": o, "summary": r.summary, "restricted": True, "projection_id": r.projection_id}
            for o, r in entries
        ]
        result.node_ids += [o for o, _ in entries]
    result.data["root"] = root
    result.data["affected"] = [nodes[n] for n in sorted(affected)]
    result.data["edges"] = _edge_rows(edges, nodes)
    owners = {o["id"]: o for n in affected for o in nodes[n]["owners"]}
    result.data["notify"] = sorted(owners.values(), key=lambda o: o["name"] or "")
    result.node_ids += [fact, *sorted(affected), *sorted(owners)]
    result.path += [PathEdge(src, rel, dst) for src, rel, dst, _ in edges]
    result.path += [PathEdge(o["id"], Rel.OWNS, n) for n in sorted(affected) for o in nodes[n]["owners"]]
    result.citations += item_citations(ctx, [item for *_, evidence in edges for item in evidence])
    return result


_AFFECTED_OWNERS = f"""
MATCH (old {{{Prop.ID}: $id}})<-[:{Rel.DEPENDS_ON}|{Rel.NEEDS}*1..{MAX_DEPTH}]-(x)
MATCH (p:{Label.PERSON})-[:{Rel.OWNS}]->(x)
RETURN DISTINCT p.{Prop.ID}
"""


def affected_owners(graph: ReadOnlyGraph, fact_id: str) -> list[str]:
    """Ungated: who might need a notice about ``fact_id``. Not a tool.

    The impact watcher uses this only to pick recipients, then calls
    ``impact_of`` with each recipient's own principal and sends only what
    that call returns. Never show this list to anyone.
    """

    return sorted(row[0] for row in graph.ro_query(_AFFECTED_OWNERS, {"id": fact_id}).result_set)


_REQUESTS = f"""
MATCH (p1:{Label.PERSON})-[:{Rel.OWNS}]->(a:{Label.COMMITMENT})-[r:{Rel.DEPENDS_ON}]->(b)
WHERE a.{Prop.STATUS} = '{Status.ACTIVE}' AND a.{Prop.PROGRESS} IN $open
{keep_visible_edges("r", carry=("p1", "a", "b"))}
RETURN p1.{Prop.DEPT}, a.{Prop.ID}, a.{Prop.PROGRESS}, b.{Prop.ID}
"""

_REQUESTS_TOTAL = f"""
MATCH (:{Label.PERSON})-[:{Rel.OWNS}]->(a:{Label.COMMITMENT})-[r:{Rel.DEPENDS_ON}]->()
WHERE a.{Prop.STATUS} = '{Status.ACTIVE}' AND a.{Prop.PROGRESS} IN $open
RETURN count(r)
"""

_BLOCKS = f"""
MATCH (x:{Label.PROJECT})-[r:{Rel.BLOCKED_BY}]->(y)
{keep_visible_edges("r", carry=("x", "y"))}
RETURN x.{Prop.ID}, y.{Prop.ID}
"""

_BLOCKS_TOTAL = f"MATCH (:{Label.PROJECT})-[r:{Rel.BLOCKED_BY}]->() RETURN count(r)"


@tool(
    "dept_rollup",
    use_when="You need counts of open cross-team requests, blocks and at-risk items between departments.",
    dont_use_when="You need the individual items behind a dependency (use get_dependencies).",
    args={
        "from_dept": "Optional requesting department, e.g. 'Sales'.",
        "to_dept": "Optional department being asked or blocking, e.g. 'Engineering'.",
    },
)
def dept_rollup(ctx: ToolContext, from_dept: str | None = None, to_dept: str | None = None) -> ToolResult:
    """Counts over visible edges only, so totals cannot leak restricted work."""

    result = ToolResult("dept_rollup", {"rows": []})
    requests = ctx.rows(_REQUESTS, open=OPEN_PROGRESS)
    blocks = ctx.rows(_BLOCKS)
    labels = _labels(ctx, [r[1] for r in requests] + [r[3] for r in requests] + [b[1] for b in blocks])
    claims = [n for n, label in labels.items() if label in CLAIM_LABELS]
    ok = visible_claims(ctx, claims)
    owners = _nodes(ctx, [r[3] for r in requests] + [n for b in blocks for n in b])

    counts: Counter = Counter()
    passed = 0
    for dept, commitment, progress, target in requests:
        if commitment not in ok or (target in claims and target not in ok):
            continue
        passed += 1
        other = owners.get(target, {}).get("dept")
        if not dept or not other or dept == other:
            continue
        counts[(dept, other, "requests")] += 1
        if progress == Progress.AT_RISK:
            counts[(dept, other, "at_risk")] += 1
        result.node_ids += [commitment, target]
        result.path.append(PathEdge(commitment, Rel.DEPENDS_ON, target))
    for blocked, blocker in blocks:
        if blocker in claims and blocker not in ok:
            continue
        passed += 1
        dept, other = owners.get(blocked, {}).get("dept"), owners.get(blocker, {}).get("dept")
        if not dept or not other or dept == other:
            continue
        counts[(dept, other, "blocks")] += 1
        result.node_ids += [blocked, blocker]
        result.path.append(PathEdge(blocked, Rel.BLOCKED_BY, blocker))

    total = ctx.rows(_REQUESTS_TOTAL, open=OPEN_PROGRESS)[0][0] + ctx.rows(_BLOCKS_TOTAL)[0][0]
    result.hidden += max(total - passed, 0)
    pairs = sorted({(a, b) for a, b, _ in counts})
    for a, b in pairs:
        if (from_dept and a.lower() != from_dept.lower()) or (to_dept and b.lower() != to_dept.lower()):
            continue
        result.data["rows"].append({
            "from_dept": a, "to_dept": b,
            "requests": counts[(a, b, "requests")], "blocks": counts[(a, b, "blocks")],
            "at_risk": counts[(a, b, "at_risk")],
        })
    return result
