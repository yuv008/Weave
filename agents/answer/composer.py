"""Turn tool results into claims, each tied to the evidence ids that support it.

``TemplateComposer`` writes claims straight from the structured tool data;
``agents.answer.llm.LLMComposer`` asks a model instead. Either way the
validator decides what survives.
"""

from dataclasses import dataclass, field
from typing import Protocol

from agents.answer.intents import Intent
from agents.answer.loop import RunState
from agents.tools.base import PathEdge
from shared.schema import Rel
from shared.timefmt import parse_ts

REL_TEXT = {Rel.DEPENDS_ON: "depends on", Rel.NEEDS: "needs", Rel.BLOCKED_BY: "is blocked by"}
MAX_SEARCH_CLAIMS = 3
FIRST_KINDS = {
    Intent.IMPACT: ("impact", "dependency"),
    Intent.OWNERSHIP: ("owner", "expert"),
    Intent.PROVENANCE: ("decision", "provenance"),
}


def path_id(edge: PathEdge) -> str:
    """Evidence id for a structural fact: a graph edge a gated tool returned (for example who owns what)."""

    return f"path:{edge.src}|{edge.rel}|{edge.dst}"


@dataclass
class Claim:
    text: str
    evidence_ids: list[str]
    kind: str = "fact"
    node_id: str | None = None
    confidence: float | None = None
    likely: bool = False
    restricted: bool = False
    freshness: str | None = None
    stale: bool = False
    flagged: bool = False
    extra: dict = field(default_factory=dict)


class Composer(Protocol):
    def compose(self, state: RunState) -> list[Claim]: ...


def day(ts: str | None) -> str:
    if not ts:
        return "unknown date"
    try:
        moment = parse_ts(ts)
    except ValueError:
        return ts
    return f"{moment:%b} {moment.day}, {moment.year}"


def _money(value, unit: str | None) -> str:
    if isinstance(value, int | float):
        number = f"{value:,.0f}" if float(value).is_integer() else f"{value:,}"
        return f"${number}" if unit == "USD" else f"{number} {unit or ''}".strip()
    return f"{value} {unit or ''}".strip()


def _restricted(card: dict, kind: str) -> Claim:
    return Claim(card["summary"], [card["projection_id"]] if card.get("projection_id") else [], kind,
                 node_id=card["id"], restricted=True)


def _decisions(data: dict) -> list[Claim]:
    out = []
    for card in data.get("decisions", []):
        if card.get("restricted"):
            out.append(_restricted(card, "decision"))
            continue
        replaces = card.get("replaces")
        if card.get("status") == "active":
            freshness = f"Active since {day(card.get('valid_from'))}"
        else:
            freshness = f"{card.get('status', '').capitalize()} ({day(card.get('valid_from'))} to {day(card.get('valid_to'))})"
        if replaces:
            freshness += f", replaces \"{replaces['statement']}\" ({day(replaces.get('valid_from'))})"
        if not card.get("statement"):
            continue
        out.append(Claim(
            card["statement"], list(card.get("evidence", [])), "decision", node_id=card["id"],
            confidence=card.get("confidence"), likely=bool(card.get("likely")), freshness=freshness,
        ))
    return out


def _figures(data: dict) -> list[Claim]:
    out = []
    for card in data.get("figures", []):
        if card.get("restricted"):
            out.append(_restricted(card, "figure"))
            continue
        freshness = f"{card['name']} as of {day(card.get('as_of'))}" + (" (stale)" if card.get("stale") else "")
        out.append(Claim(
            f"{card['name']}: {_money(card.get('value'), card.get('unit'))}", list(card.get("evidence", [])),
            "figure", node_id=card["id"], confidence=card.get("confidence"), likely=bool(card.get("likely")),
            freshness=freshness, stale=bool(card.get("stale")),
        ))
    return out


def _trace(data: dict) -> list[Claim]:
    decision = data.get("decision") or {}
    if decision.get("restricted"):
        return [_restricted(decision, "decision")]
    out = []
    for ev in data.get("evidence", []):
        quote = f": \"{ev['quote']}\"" if ev.get("quote") else ""
        out.append(Claim(
            f"{ev.get('author') or 'Someone'} in {ev.get('source') or 'a source'} on {day(ev.get('at'))}{quote}",
            [ev["item_id"]], "provenance", node_id=decision.get("id"),
        ))
    return out


def _owners(data: dict) -> list[Claim]:
    subject = (data.get("subject") or {}).get("name", "it")
    experts = {e["id"]: e for e in data.get("experts", [])}
    out = []
    for owner in data.get("owners", []):
        evidence = [path_id(PathEdge(owner["id"], Rel.OWNS, owner["owns"])),
                    *experts.get(owner["id"], {}).get("evidence", [])]
        dept = f" ({owner['dept']})" if owner.get("dept") else ""
        out.append(Claim(f"{owner['name']}{dept} owns {subject}.", evidence, "owner", node_id=owner["id"]))
    for expert in data.get("experts", []):
        dept = f" ({expert['dept']})" if expert.get("dept") else ""
        out.append(Claim(
            f"{expert['name']}{dept} is active on {subject} ({expert['items']} items you can see).",
            list(expert.get("evidence", [])), "expert", node_id=expert["id"],
        ))
    return out


def _node_text(node: dict) -> str:
    text = node.get("name") or node.get("summary") or node.get("id")
    owners = ", ".join(o["name"] for o in node.get("owners", []) if o.get("name"))
    details = [d for d in (f"owner {owners}" if owners else None, node.get("dept")) if d]
    return f"\"{text}\"" + (f" ({', '.join(details)})" if details else "")


def _edges(data: dict, kind: str) -> list[Claim]:
    nodes = {n["id"]: n for n in data.get("nodes", []) + data.get("affected", [])}
    if data.get("root"):
        nodes[data["root"]["id"]] = data["root"]
    out = []
    for edge in data.get("edges", []):
        src, dst = nodes.get(edge["src"], {"id": edge["src"]}), nodes.get(edge["dst"], {"id": edge["dst"]})
        text = f"{_node_text(src)} {REL_TEXT.get(edge['rel'], edge['rel'].lower())} {_node_text(dst)}."
        if edge.get("cross_department"):
            text += f" Crosses {edge['src_dept']} to {edge['dst_dept']}."
        out.append(Claim(text, list(edge.get("evidence", [])), kind, node_id=edge["src"]))
    return out


def _impact(data: dict) -> list[Claim]:
    out = []
    root = data.get("root") or {}
    if root.get("restricted") and root.get("projection_id"):
        out.append(_restricted(root, "impact"))
    return out + _edges(data, "impact")


def _rollup(data: dict) -> list[Claim]:
    return [
        Claim(f"{row['from_dept']} to {row['to_dept']}: {row['requests']} open requests, {row['blocks']} blocks, "
              f"{row['at_risk']} at risk.", [], "rollup")
        for row in data.get("rows", [])
    ]


def _search(data: dict) -> list[Claim]:
    return [
        Claim(f"{hit.get('author') or 'Someone'} in {hit.get('source') or 'a source'} on {day(hit.get('at'))}: "
              f"\"{hit.get('snippet') or ''}\"", [hit["item_id"]], "search", node_id=hit["item_id"])
        for hit in data.get("hits", [])[:MAX_SEARCH_CLAIMS]
    ]


BUILDERS = {
    "get_decision": _decisions,
    "get_figures": _figures,
    "trace_decision": _trace,
    "find_owner": _owners,
    "get_dependencies": lambda data: _edges(data, "dependency"),
    "impact_of": _impact,
    "dept_rollup": _rollup,
    "search_context": _search,
}


class TemplateComposer:
    def compose(self, state: RunState) -> list[Claim]:
        claims, seen = [], set()
        for result in state.results:
            for claim in BUILDERS.get(result.tool, lambda data: [])(result.data):
                key = (claim.text, tuple(claim.evidence_ids))
                if key not in seen:
                    seen.add(key)
                    claims.append(claim)
        first_kinds = FIRST_KINDS.get(state.intent, ())
        return sorted(claims, key=lambda c: c.kind not in first_kinds)
