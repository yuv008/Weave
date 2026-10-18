"""Question intents and the tools each one may use.

The intent limits the tool set before any planning happens, so a lookup can
never draft an email and a draft can never walk ownership. ``recall_memory``
runs before routing for every intent (it reads only the caller's own memory).
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class Intent(StrEnum):
    LOOKUP = "lookup"
    PROVENANCE = "provenance"
    OWNERSHIP = "ownership"
    IMPACT = "impact"
    DRAFT = "draft"


ALLOWED: dict[Intent, frozenset[str]] = {
    Intent.LOOKUP: frozenset({"get_decision", "get_figures", "search_context", "recall_memory"}),
    Intent.PROVENANCE: frozenset({"get_decision", "trace_decision", "search_context", "recall_memory"}),
    Intent.OWNERSHIP: frozenset({"find_owner", "search_context", "recall_memory"}),
    Intent.IMPACT: frozenset({"get_dependencies", "impact_of", "dept_rollup", "get_decision", "search_context"}),
    Intent.DRAFT: frozenset({
        "get_decision", "get_figures", "get_dependencies", "search_context", "draft_email", "notify_owner",
    }),
}


@dataclass(frozen=True)
class Route:
    intent: Intent
    reason: str


class Router(Protocol):
    def route(self, question: str) -> Route: ...


_RULES: tuple[tuple[Intent, re.Pattern], ...] = tuple(
    (intent, re.compile(pattern, re.IGNORECASE))
    for intent, pattern in (
        (Intent.LOOKUP, r"\b(api key|openai key|current profit|profit is|grown from|earlier profit)\b"),
        (Intent.DRAFT, r"\b(draft|compose|write (an?|the) (email|note|update|message)|email|status update"
                       r"|notify|let \w+ know|tell \w+ (about|that))\b"),
        (Intent.IMPACT, r"\b(affect(s|ed)?|impact(s|ed)?|depends? on|dependen\w*|blocked|blocking|blocks"
                        r"|break(s)?|rests? on|downstream|upstream|at risk|cross-team|requests between)\b"),
        (Intent.OWNERSHIP, r"\b(who owns|owners?|who is responsible|responsible for|who should i ask|who knows"
                           r"|who to ask|point of contact|experts?|who works on|who can help)\b"),
        (Intent.PROVENANCE, r"\b(why|who decided|who made|sources?|evidence|provenance|where did|how do we know"
                            r"|trace|history of|when was|based on|what changed)\b"),
    )
)


class RuleRouter:
    """Keyword rules, most specific first. Anything unmatched is a lookup."""

    def route(self, question: str) -> Route:
        for intent, pattern in _RULES:
            match = pattern.search(question or "")
            if match:
                return Route(intent, f"matched '{match.group(0).lower()}'")
        return Route(Intent.LOOKUP, "no rule matched")
