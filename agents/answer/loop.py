"""The bounded tool loop. The guards hold for any planner, rule-based or LLM.

- at most ``answer.max_tool_calls`` tool calls per question (recall included)
- the tool must be allowed for the question's intent
- an id-shaped argument must already have appeared in this run (a subject
  match, a recalled memory or an earlier result), so a planner cannot invent ids
- arguments are checked by ``invoke`` like any other caller's

A refused step is recorded in the trace and never reaches a tool.
"""

import logging
import time
from dataclasses import dataclass, field
from typing import Protocol

from agents.answer.intents import Intent
from agents.answer.subjects import Match
from agents.gate.principal import Principal
from agents.tools.base import ToolContext, ToolInputError, ToolResult
from agents.tools.invoke import ToolCache, invoke

log = logging.getLogger("weave.answer")

CHANGE_SUFFIXES = ("superseded", "retracted", "contradicts")


@dataclass(frozen=True)
class ToolCall:
    tool: str
    args: dict
    why: str = ""


@dataclass(frozen=True)
class Finish:
    reason: str = ""


@dataclass
class Step:
    tool: str
    args: dict
    status: str
    """``ok``, ``rejected``, ``bad_input`` or ``error``."""
    reason: str = ""
    node_ids: list[str] = field(default_factory=list)
    hidden: int = 0
    ms: int = 0

    def public(self) -> dict:
        out = {"tool": self.tool, "args": self.args, "status": self.status}
        if self.reason:
            out["reason"] = self.reason
        return out


@dataclass
class RunState:
    question: str
    intent: Intent
    principal: Principal
    subjects: list[Match] = field(default_factory=list)
    results: list[ToolResult] = field(default_factory=list)
    trace: list[Step] = field(default_factory=list)
    known_ids: set[str] = field(default_factory=set)
    calls: int = 0
    cursor: int = 0
    """Position in the rule planner's playbook."""
    playbook: list[str] = field(default_factory=list)
    history: list = field(default_factory=list)
    """Earlier conversation turns, already re-gated."""
    seed_facts: list[str] = field(default_factory=list)
    """Claim ids the caller handed in (already gated); a draft states these first."""

    def by_tool(self, name: str) -> list[ToolResult]:
        return [r for r in self.results if r.tool == name]

    def has_facts(self) -> bool:
        return any(r.citations or r.path for r in self.results if r.tool != "recall_memory")

    @property
    def hidden(self) -> int:
        return sum(step.hidden for step in self.trace)


class Planner(Protocol):
    def next_step(self, state: RunState) -> ToolCall | Finish: ...


def _id_args(tenant: str, args: dict) -> list[str]:
    values = []
    for value in args.values():
        for v in value if isinstance(value, list) else [value]:
            if isinstance(v, str) and v.startswith(f"{tenant}:"):
                values.append(v)
    return values


def _known(value: str, known: set[str]) -> bool:
    if value in known:
        return True
    head, _, suffix = value.rpartition(":")
    return suffix in CHANGE_SUFFIXES and head in known


def guard(state: RunState, call: ToolCall, allowed: frozenset[str], max_calls: int) -> str | None:
    """Why ``call`` is refused, or ``None`` if it may run."""

    if state.calls >= max_calls:
        return f"limit of {max_calls} tool calls reached"
    if call.tool not in allowed:
        return f"{call.tool} is not allowed for a {state.intent} question"
    if not isinstance(call.args, dict):
        return "arguments must be an object"
    unknown = [v for v in _id_args(state.principal.tenant, call.args) if not _known(v, state.known_ids)]
    if unknown:
        return f"ids not seen in this run: {unknown}"
    return None


def execute(state: RunState, ctx: ToolContext, call: ToolCall, cache: ToolCache | None) -> ToolResult | None:
    """Run one call that passed the guards; record it either way."""

    started = time.monotonic()
    state.calls += 1
    try:
        result = invoke(call.tool, ctx, call.args, cache)
    except ToolInputError as exc:
        state.trace.append(Step(call.tool, call.args, "bad_input", str(exc), ms=_ms(started)))
        return None
    except Exception:
        log.exception("tool %s failed in the answer loop", call.tool)
        state.trace.append(Step(call.tool, call.args, "error", "internal error", ms=_ms(started)))
        return None
    state.results.append(result)
    state.known_ids.update(result.node_ids)
    state.known_ids.update(c.projection_id for c in result.citations if hasattr(c, "projection_id"))
    state.known_ids.update(c.item_id for c in result.citations if hasattr(c, "item_id"))
    state.trace.append(Step(call.tool, call.args, "ok", node_ids=list(result.node_ids), hidden=result.hidden,
                            ms=_ms(started)))
    return result


def run_loop(
    state: RunState,
    ctx: ToolContext,
    planner: Planner,
    *,
    allowed: frozenset[str],
    max_calls: int,
    cache: ToolCache | None = None,
) -> RunState:
    for _ in range(max_calls * 3):
        call = planner.next_step(state)
        if isinstance(call, Finish):
            break
        reason = guard(state, call, allowed, max_calls)
        if reason is not None:
            state.trace.append(Step(call.tool, call.args, "rejected", reason))
            if state.calls >= max_calls:
                break
            continue
        execute(state, ctx, call, cache)
    return state


def _ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)
