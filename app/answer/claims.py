"""Caller-supplied commitments and figures.

The quote has to appear in the stored excerpt of one item. Repeating the
same evidence and statement merges. Supersede adds a new claim and leaves
the old one inactive.
"""

from datetime import UTC, datetime

from shared.ids import claim_id
from shared.schema import COMMITMENT_PROGRESS, Label, Prop, Rel, Status
from shared.timefmt import to_ts

EXTRACTOR_VERSION = "caller-1"


def record_commitment(graph, store, tenant_id: str, body: dict) -> str:
    statement = _text(body.get("statement"), "statement is required")
    due = _text(body.get("due"), "due is required")
    progress = _text(body.get("progress"), "progress is required")
    if progress not in COMMITMENT_PROGRESS:
        raise ValueError("progress is not a commitment state")
    item_id, quote, start, end, valid_from = _evidence(graph, tenant_id, body)
    fact_id = claim_id(tenant_id, Label.COMMITMENT, item_id, statement)
    _write_claim(
        graph,
        label=Label.COMMITMENT,
        fact_id=fact_id,
        tenant_id=tenant_id,
        item_id=item_id,
        quote=quote,
        span=[start, end],
        valid_from=valid_from,
        fields={
            Prop.STATEMENT: statement,
            Prop.DUE: due,
            Prop.PROGRESS: progress,
        },
    )
    return fact_id


def record_figure(graph, store, tenant_id: str, body: dict) -> str:
    name = _text(body.get("name"), "name is required")
    unit = _text(body.get("unit"), "unit is required")
    as_of = _text(body.get("as_of"), "as_of is required")
    value = body.get("value")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("value is required")
    item_id, quote, start, end, valid_from = _evidence(graph, tenant_id, body)
    statement = f"{name} {value} {unit}"
    fact_id = claim_id(tenant_id, Label.FIGURE, item_id, statement)
    _write_claim(
        graph,
        label=Label.FIGURE,
        fact_id=fact_id,
        tenant_id=tenant_id,
        item_id=item_id,
        quote=quote,
        span=[start, end],
        valid_from=valid_from,
        fields={
            Prop.NAME: name,
            Prop.VALUE: value,
            Prop.UNIT: unit,
            Prop.AS_OF: as_of,
            Prop.STATEMENT: statement,
        },
    )
    return fact_id


def supersede_claim(graph, store, tenant_id: str, body: dict) -> dict:
    old_id = _text(body.get("old_id"), "old_id is required")
    kind = _text(body.get("kind"), "kind is required")
    rows = graph.query(
        f"MATCH (c {{{Prop.ID}: $id}}) RETURN c.{Prop.STATUS}, c.{Prop.TENANT_ID}",
        {"id": old_id},
    ).result_set
    if not rows or rows[0][1] != tenant_id:
        raise ValueError("claim was not found")
    if rows[0][0] != Status.ACTIVE:
        raise ValueError("only an active claim can be superseded")
    if kind == "commitment":
        new_id = record_commitment(graph, store, tenant_id, body)
    elif kind == "figure":
        new_id = record_figure(graph, store, tenant_id, body)
    else:
        raise ValueError("kind must be commitment or figure")
    if new_id == old_id:
        raise ValueError("a claim cannot supersede itself")
    when = to_ts(datetime.now(UTC))
    graph.query(
        f"""
        MATCH (new {{{Prop.ID}: $new}}), (old {{{Prop.ID}: $old}})
        MERGE (new)-[:{Rel.SUPERSEDES}]->(old)
        SET old.{Prop.STATUS} = $status, old.{Prop.VALID_TO} = $valid_to
        """,
        {"new": new_id, "old": old_id, "status": Status.SUPERSEDED, "valid_to": when},
    )
    store.record_change(tenant_id, old_id, "superseded", new_id)
    return {"id": new_id, "old_id": old_id, "event_id": f"{old_id}:superseded"}


def _evidence(graph, tenant_id: str, body: dict):
    item_id = _text(body.get("item_id"), "item_id is required")
    quote = _text(body.get("quote"), "quote is required")
    rows = graph.query(
        f"""
        MATCH (n:{Label.ITEM} {{{Prop.ID}: $id}})
        RETURN n.{Prop.EXCERPT}, n.{Prop.OCCURRED_AT}, n.{Prop.TENANT_ID}, coalesce(n.{Prop.DELETED}, false)
        """,
        {"id": item_id},
    ).result_set
    if not rows or rows[0][2] != tenant_id or rows[0][3]:
        raise ValueError("item was not found")
    excerpt = rows[0][0] or ""
    if quote not in excerpt:
        raise ValueError("quote was not found in the excerpt")
    start = excerpt.index(quote)
    valid_from = rows[0][1] or to_ts(datetime.now(UTC))
    return item_id, quote, start, start + len(quote), valid_from


def _write_claim(graph, *, label, fact_id, tenant_id, item_id, quote, span, valid_from, fields) -> None:
    assignments = ", ".join(f"c.{name} = ${name}" for name in fields)
    graph.query(
        f"""
        MERGE (c:{label} {{{Prop.ID}: $id}})
        SET c.{Prop.TENANT_ID} = $tenant_id,
            c.{Prop.STATUS} = $status,
            c.{Prop.VALID_FROM} = $valid_from,
            c.{Prop.CONFIDENCE} = $confidence,
            c.{Prop.LAST_VERIFIED_AT} = $verified,
            c.{Prop.EXTRACTOR_VERSION} = $extractor,
            {assignments}
        WITH c
        MATCH (n:{Label.ITEM} {{{Prop.ID}: $item_id}})
        MERGE (c)-[e:{Rel.EVIDENCED_BY}]->(n)
        SET e.{Prop.QUOTE} = $quote, e.{Prop.SPAN} = $span, e.{Prop.CONFIDENCE} = $confidence
        """,
        {
            "id": fact_id,
            "tenant_id": tenant_id,
            "status": Status.ACTIVE,
            "valid_from": valid_from,
            "confidence": 1.0,
            "verified": to_ts(datetime.now(UTC)),
            "extractor": EXTRACTOR_VERSION,
            "item_id": item_id,
            "quote": quote,
            "span": span,
            **fields,
        },
    )


def _text(value, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value.strip()
