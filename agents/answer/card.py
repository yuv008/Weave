"""The answer card: answer, evidence per claim, path, freshness, confidence.

Evidence shows quote, author, date, source and link, or for a projection the
approved summary only (source "restricted", no quote, no link). The hidden
count appears only when ``hidden_sources_notice`` is on.
"""

from dataclasses import asdict, dataclass, field

from agents.answer.composer import Claim, path_id
from agents.answer.loop import RunState
from agents.tools.base import PathEdge
from shared.config import WeaveConfig

EMPTY_ANSWER = "I couldn't find anything on that which you can see."
DRAFT_ANSWER = "Draft ready for your review. Nothing has been sent."
NOTIFY_ANSWER = "Message proposed. Confirm it to queue the Slack DM; nothing has been sent."
ANSWER_CLAIMS = 2
MAX_PATH = 40


@dataclass
class AnswerCard:
    question: str
    intent: str
    answer: str
    claims: list[Claim] = field(default_factory=list)
    evidence: dict[str, dict] = field(default_factory=dict)
    path: list[PathEdge] = field(default_factory=list)
    freshness: list[str] = field(default_factory=list)
    confidence: float | None = None
    likely: bool = False
    conflict: bool = False
    conflicts: list[str] = field(default_factory=list)
    proposal: dict | None = None
    trace: list[dict] = field(default_factory=list)
    hidden: int = 0
    show_hidden: bool = False
    dropped: int = 0
    conversation_id: str | None = None
    episode_id: str | None = None
    request_id: str | None = None

    @property
    def node_ids(self) -> list[str]:
        ids = [c.node_id for c in self.claims if c.node_id]
        for evidence in (e for c in self.claims for e in c.evidence_ids):
            if evidence.startswith("path:"):
                src, _, dst = evidence[len("path:"):].split("|", 2)
                ids += [src, dst]
            else:
                ids.append(evidence)
        return list(dict.fromkeys(ids))

    def public(self) -> dict:
        out = {
            "question": self.question,
            "intent": self.intent,
            "answer": self.answer,
            "claims": [
                {k: v for k, v in {
                    "text": c.text, "evidence": c.evidence_ids, "kind": c.kind, "confidence": c.confidence,
                    "likely": c.likely or None, "restricted": c.restricted or None,
                    "unverified": c.flagged or None, "stale": c.stale or None,
                }.items() if v is not None}
                for c in self.claims
            ],
            "evidence": self.evidence,
            "path": [asdict(e) for e in self.path],
            "freshness": self.freshness,
            "confidence": self.confidence,
            "likely": self.likely,
            "conflict": self.conflict,
            "trace": self.trace,
        }
        if self.conflicts:
            out["conflicts"] = self.conflicts
        if self.proposal:
            out["proposal"] = self.proposal
        for key in ("conversation_id", "episode_id", "request_id"):
            if getattr(self, key):
                out[key] = getattr(self, key)
        if self.show_hidden and self.hidden:
            out["hidden_count"] = self.hidden
        return out


def _evidence(state: RunState) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for result in state.results:
        for edge in result.path:
            out.setdefault(path_id(edge), {"kind": "path", "src": edge.src, "rel": edge.rel, "dst": edge.dst})
        for c in result.citations:
            if hasattr(c, "projection_id"):
                out.setdefault(c.projection_id, {"kind": "projection", "summary": c.summary, "source": c.source})
            else:
                out.setdefault(c.item_id, {"kind": "evidence", "quote": c.quote, "author": c.author, "at": c.at,
                                           "source": c.source, "url": c.url})
    return out


def _proposal(state: RunState) -> dict | None:
    for result in reversed(state.results):
        if result.tool in ("draft_email", "notify_owner") and result.data.get("proposal_id"):
            return {"tool": result.tool, **result.data}
    return None


def build_card(state: RunState, claims: list[Claim], dropped: int, cfg: WeaveConfig) -> AnswerCard:
    cited = {e for c in claims for e in c.evidence_ids}
    evidence = {k: v for k, v in _evidence(state).items() if k in cited}
    proposal = _proposal(state)
    if proposal is not None:
        answer = DRAFT_ANSWER if proposal["tool"] == "draft_email" else NOTIFY_ANSWER
    elif claims:
        answer = " ".join(c.text if c.text.endswith(".") else f"{c.text}." for c in claims[:ANSWER_CLAIMS])
    else:
        answer = EMPTY_ANSWER

    path: list[PathEdge] = []
    for result in state.results:
        for edge in result.path:
            if edge not in path:
                path.append(edge)

    confidences = [c.confidence for c in claims if c.confidence is not None]
    confidence = min(confidences) if confidences else None
    decisions = [r for r in state.results if r.tool == "get_decision"]
    conflict = any(r.data.get("conflict") for r in decisions)
    conflicts = [c.text for c in claims if c.kind == "decision" and not c.restricted] if conflict else []
    return AnswerCard(
        question=state.question,
        intent=str(state.intent),
        answer=answer,
        claims=claims,
        evidence=evidence,
        path=path[:MAX_PATH],
        freshness=[c.freshness for c in claims if c.freshness],
        confidence=confidence,
        likely=any(c.likely for c in claims) or (confidence is not None and confidence < cfg.likely_threshold),
        conflict=conflict,
        conflicts=conflicts,
        proposal=proposal,
        trace=[s.public() for s in state.trace],
        hidden=state.hidden,
        show_hidden=cfg.hidden_sources_notice,
        dropped=dropped,
    )
