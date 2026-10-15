"""The LLM path, behind ``answer.llm.provider``. No provider is chosen yet.

``LLM`` is the one interface a provider adapter implements. ``LLMRouter``,
``LLMPlanner`` and ``LLMComposer`` sit behind it; the loop, its guards and
the citation validator are the same as on the rule path, so a model can only
call tools the intent allows, only with ids already seen, and only claims
with evidence from this run survive. ``ScriptedLLM`` drives the tests.

Adding a provider: write a class with ``complete`` (OpenAI-style messages and
function tools in, ``LLMReply`` out), then construct it in ``build_components``.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Protocol

from agents.answer.composer import Claim, TemplateComposer
from agents.answer.intents import ALLOWED, Intent, Route, RuleRouter
from agents.answer.loop import Finish, RunState, ToolCall
from agents.answer.planner import RulePlanner
from agents.mcp.schema import description, input_schema
from agents.tools.base import REGISTRY
from shared.config import WeaveConfig

RESULT_CHARS = 6000

ROUTE_SCHEMA = {
    "type": "object",
    "properties": {"intent": {"type": "string", "enum": [str(i) for i in Intent]}},
    "required": ["intent"],
    "additionalProperties": False,
}

CLAIMS_SCHEMA = {
    "type": "object",
    "properties": {
        "claims": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "text": {"type": "string"},
                    "evidence_ids": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["text", "evidence_ids"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["claims"],
    "additionalProperties": False,
}

ROUTER_PROMPT = (
    "Classify the question for a company knowledge assistant. lookup: what is the current decision or number. "
    "provenance: why, who decided, sources. ownership: who owns or knows about something. impact: what depends "
    "on or is affected by something. draft: write an email or notify a colleague. Reply with JSON."
)

PLANNER_PROMPT = (
    "You answer questions about the company by calling tools. Call one tool at a time; stop (reply without a "
    "tool call) when you have enough. Use ids exactly as tools returned them; never invent ids. "
    "At most {max_calls} tool calls in total. Suggested steps for this kind of question: {playbook}."
)

COMPOSER_PROMPT = (
    "Write the answer as short claims. Every claim must list the evidence ids that support it, taken from the "
    "tool results' citations (item_id or projection_id), or 'path:src|rel|dst' for an edge in a result's path "
    "(for structural facts such as ownership). A restricted citation is an approved summary: state it "
    "without quoting. Do not state anything the results do not support. Reply with JSON."
)


@dataclass(frozen=True)
class LLMToolCall:
    name: str
    arguments: dict


@dataclass
class LLMReply:
    text: str = ""
    tool_calls: list[LLMToolCall] = field(default_factory=list)
    data: dict | None = None
    """Parsed JSON when a ``json_schema`` was requested."""


class LLM(Protocol):
    def complete(self, messages: list[dict], tools: list[dict] | None = None,
                 json_schema: dict | None = None) -> LLMReply: ...


class ScriptedLLM:
    """Replies from a script, in order; a callable entry computes its reply from the request."""

    def __init__(self, replies: list[LLMReply | Callable[..., LLMReply]]):
        self._replies = list(replies)
        self.requests: list[dict] = []

    def complete(self, messages, tools=None, json_schema=None) -> LLMReply:
        self.requests.append({"messages": messages, "tools": tools, "json_schema": json_schema})
        if not self._replies:
            return LLMReply()
        reply = self._replies.pop(0)
        return reply(messages, tools, json_schema) if callable(reply) else reply


def function_tools(names) -> list[dict]:
    return [
        {"type": "function", "function": {
            "name": name, "description": description(REGISTRY[name]), "parameters": input_schema(REGISTRY[name]),
        }}
        for name in sorted(names) if name in REGISTRY
    ]


class LLMRouter:
    def __init__(self, llm: LLM, fallback=None):
        self.llm = llm
        self.fallback = fallback or RuleRouter()

    def route(self, question: str) -> Route:
        try:
            reply = self.llm.complete(
                [{"role": "system", "content": ROUTER_PROMPT}, {"role": "user", "content": question}],
                json_schema=ROUTE_SCHEMA,
            )
            return Route(Intent((reply.data or {})["intent"]), "llm")
        except Exception:
            return self.fallback.route(question)


def _result_text(result, cfg: WeaveConfig) -> str:
    text = json.dumps(result.public(cfg), ensure_ascii=False, default=str)
    return text if len(text) <= RESULT_CHARS else text[:RESULT_CHARS] + "...(truncated)"


def transcript(state: RunState, cfg: WeaveConfig) -> list[dict]:
    """The question, subjects and every step so far, as chat messages."""

    subjects = ", ".join(f"{m.name} ({m.label}, id {m.id})" for m in state.subjects) or "none matched"
    content = f"{state.question}\n\nSubjects: {subjects}"
    if state.seed_facts:
        content += f"\nFacts to state first: {', '.join(state.seed_facts)}"
    messages = [{"role": "user", "content": content}]
    results = iter(state.results)
    for i, step in enumerate(state.trace):
        call_id = f"call_{i}"
        messages.append({"role": "assistant", "content": "", "tool_calls": [{
            "id": call_id, "type": "function",
            "function": {"name": step.tool, "arguments": json.dumps(step.args, default=str)},
        }]})
        content = _result_text(next(results), cfg) if step.status == "ok" else f"{step.status}: {step.reason}"
        messages.append({"role": "tool", "tool_call_id": call_id, "content": content})
    return messages


class LLMPlanner:
    """Function calling over the intent's tools only."""

    def __init__(self, llm: LLM, cfg: WeaveConfig):
        self.llm = llm
        self.cfg = cfg

    def next_step(self, state: RunState) -> ToolCall | Finish:
        allowed = ALLOWED[state.intent] - {"recall_memory"}
        system = PLANNER_PROMPT.format(max_calls=self.cfg.answer.max_tool_calls,
                                       playbook=", ".join(state.playbook) or "none")
        reply = self.llm.complete([{"role": "system", "content": system}, *transcript(state, self.cfg)],
                                  tools=function_tools(allowed))
        if not reply.tool_calls:
            return Finish(reply.text or "model finished")
        call = reply.tool_calls[0]
        return ToolCall(call.name, dict(call.arguments or {}), "llm")


class LLMComposer:
    """JSON claims with evidence ids; falls back to the template composer if the reply is unusable."""

    def __init__(self, llm: LLM, cfg: WeaveConfig, fallback=None):
        self.llm = llm
        self.cfg = cfg
        self.fallback = fallback or TemplateComposer()

    def compose(self, state: RunState) -> list[Claim]:
        try:
            reply = self.llm.complete(
                [{"role": "system", "content": COMPOSER_PROMPT}, *transcript(state, self.cfg)],
                json_schema=CLAIMS_SCHEMA,
            )
            raw = (reply.data or {})["claims"]
            claims = [Claim(str(c["text"]), [str(e) for e in c["evidence_ids"]], "llm") for c in raw]
        except Exception:
            return self.fallback.compose(state)
        try:
            known = TemplateComposer().compose(state)
        except Exception:
            known = []
        return [self._enrich(c, known) for c in claims]

    @staticmethod
    def _enrich(claim: Claim, known: list[Claim]) -> Claim:
        """Confidence, freshness and labels from the template claims that share its evidence."""

        related = [k for k in known if set(k.evidence_ids) & set(claim.evidence_ids)]
        confidences = [k.confidence for k in related if k.confidence is not None]
        claim.confidence = min(confidences) if confidences else None
        claim.likely = any(k.likely for k in related)
        claim.restricted = bool(related) and all(k.restricted for k in related)
        claim.stale = any(k.stale for k in related)
        claim.freshness = next((k.freshness for k in related if k.freshness), None)
        claim.kind = related[0].kind if related else "llm"
        return claim


def build_components(cfg: WeaveConfig, llm: LLM | None = None):
    """Router, planner and composer for ``answer.llm.provider``; ``none`` is the rule-based set."""

    provider = cfg.answer.llm.provider
    if llm is None:
        if provider == "none":
            return RuleRouter(), RulePlanner(), TemplateComposer()
        raise NotImplementedError(f"no adapter for LLM provider {provider!r} yet; set answer.llm.provider: none")
    return LLMRouter(llm), LLMPlanner(llm, cfg), LLMComposer(llm, cfg)
