"""Structured decisions read from stored Slack excerpts.

The caller supplies the statement and the quote. A quote that is not in the
excerpt is not returned. Each hit is merged as one Decision. Calling again
does not add a second node for the same message and statement.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from app.models.canonical import EXCERPT_LIMIT
from shared.ids import claim_id
from shared.schema import Label, Prop, Rel, Status
from shared.timefmt import to_ts

EXTRACTOR_VERSION = "caller-1"

_QUERY = """
MATCH (m:Message)
WHERE m.tenant_id = $tenant_id
  AND m.external_id STARTS WITH $channel_prefix
  AND coalesce(m.deleted, false) = false
  AND coalesce(m.placeholder, false) = false
  AND m.excerpt CONTAINS $quote
RETURN m.id, m.excerpt, m.occurred_at, m.source_url
ORDER BY m.occurred_at DESC
LIMIT 20
"""


@dataclass(frozen=True)
class DecisionEvidence:
    quote: str
    source_url: str
    message_id: str

    def as_dict(self) -> dict[str, str]:
        return {
            "quote": self.quote,
            "source_url": self.source_url,
            "message_id": self.message_id,
        }


@dataclass(frozen=True)
class ChannelDecision:
    statement: str
    status: str
    valid_from: str
    evidence: DecisionEvidence

    def as_dict(self) -> dict[str, object]:
        return {
            "statement": self.statement,
            "status": self.status,
            "valid_from": self.valid_from,
            "evidence": self.evidence.as_dict(),
        }


def channel_decisions(graph, tenant_id: str, channel_id: str, statement: str, quote: str) -> list[ChannelDecision]:
    statement = _required(statement, "statement is required")
    quote = _required(quote, "quote is required")
    channel_id = _required(channel_id, "channel_id is required")
    if len(quote) > EXCERPT_LIMIT:
        raise ValueError("quote is longer than an excerpt")
    rows = graph.query(
        _QUERY,
        {
            "tenant_id": tenant_id,
            "channel_prefix": f"{channel_id}:",
            "quote": quote,
        },
    ).result_set
    decisions: list[ChannelDecision] = []
    for row in rows:
        excerpt = row[1] or ""
        if quote not in excerpt:
            continue
        start = excerpt.find(quote)
        decision = ChannelDecision(
            statement=statement,
            status=Status.ACTIVE,
            valid_from=row[2] or "",
            evidence=DecisionEvidence(
                quote=quote,
                source_url=row[3] or "",
                message_id=row[0] or "",
            ),
        )
        _store_decision(graph, tenant_id, decision, start, start + len(quote))
        decisions.append(decision)
    return decisions


def _store_decision(graph, tenant_id: str, decision: ChannelDecision, start: int, end: int) -> None:
    graph.query(
        f"""
        MERGE (d:{Label.DECISION} {{id: $id}})
        SET d.{Prop.TENANT_ID} = $tenant_id,
            d.{Prop.STATEMENT} = $statement,
            d.{Prop.STATUS} = $status,
            d.{Prop.VALID_FROM} = $valid_from,
            d.{Prop.CONFIDENCE} = $confidence,
            d.{Prop.LAST_VERIFIED_AT} = $last_verified_at,
            d.{Prop.EXTRACTOR_VERSION} = $extractor_version
        MERGE (m:{Label.MESSAGE} {{id: $message_id}})
        MERGE (d)-[e:{Rel.EVIDENCED_BY}]->(m)
        SET e.{Prop.QUOTE} = $quote,
            e.{Prop.SPAN} = $span,
            e.{Prop.CONFIDENCE} = $confidence
        """,
        {
            "id": claim_id(tenant_id, Label.DECISION, decision.evidence.message_id, decision.statement),
            "tenant_id": tenant_id,
            "statement": decision.statement,
            "status": decision.status,
            "valid_from": decision.valid_from,
            "confidence": 1.0,
            "last_verified_at": to_ts(datetime.now(UTC)),
            "extractor_version": EXTRACTOR_VERSION,
            "message_id": decision.evidence.message_id,
            "quote": decision.evidence.quote,
            "span": [start, end],
        },
    )


def _required(value: object, message: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(message)
    return value.strip()
