"""The citation contract: a claim stands only on evidence returned in this run.

An evidence id counts when it is a citation item id, a projection id, or a
path id (a graph edge, for structural facts such as ownership) from a tool
result of this run, so a claim cannot cite something the caller was never
shown. ``drop`` removes any other claim; ``flag`` keeps it marked unverified.
"""

from dataclasses import replace

from agents.answer.composer import Claim, path_id
from agents.answer.loop import RunState


def cited_ids(state: RunState) -> set[str]:
    ids = set()
    for result in state.results:
        for c in result.citations:
            ids.add(getattr(c, "item_id", None) or getattr(c, "projection_id", None))
        ids.update(path_id(edge) for edge in result.path)
    ids.discard(None)
    return ids


def validate(claims: list[Claim], state: RunState, policy: str) -> tuple[list[Claim], int]:
    """Claims that keep their place, and how many were dropped."""

    if policy not in ("drop", "flag"):
        raise ValueError("citation policy must be 'drop' or 'flag'")
    allowed = cited_ids(state)
    kept, dropped = [], 0
    for claim in claims:
        evidence = list(dict.fromkeys(claim.evidence_ids))
        if evidence and all(e in allowed for e in evidence):
            kept.append(replace(claim, evidence_ids=evidence, flagged=False))
        elif policy == "flag":
            kept.append(replace(claim, evidence_ids=[e for e in evidence if e in allowed], flagged=True))
        else:
            dropped += 1
    return kept, dropped
