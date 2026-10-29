"""The LLM path, behind ``answer.llm.provider``.

``LLM`` is the one interface a provider adapter implements. ``LLMRouter``,
``LLMPlanner`` and ``LLMComposer`` sit behind it; the loop, its guards and
the citation validator are the same as on the rule path, so a model can only
call tools the intent allows, only with ids already seen, and only claims
with evidence from this run survive. ``provider: none`` keeps the rule-based
components. ``provider: groq`` uses ``GroqLLM`` and the key named by
``api_key_env`` (``GROQ_API_KEY``). Groq calls share a process-local token
bucket (``answer.llm.rpm``) and wait when it is empty. ``ScriptedLLM`` drives
the tests and does not take a slot.
"""

import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Protocol

from agents.answer.composer import Claim, TemplateComposer
from agents.answer.intents import ALLOWED, Intent, Route, RuleRouter
from agents.answer.loop import Finish, RunState, ToolCall
from agents.answer.planner import RulePlanner, blank_search
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
    "Write one or two sentences that answer the question. State the fact. Do not name the author, the source, "
    "the channel, or the date, and do not wrap the fact in quotation marks. Every claim must list the evidence "
    "ids that support it, taken from the tool results' citations (item_id or projection_id), or "
    "'path:src|rel|dst' for an edge in a result's path (for structural facts such as ownership). A restricted "
    "citation is an approved summary: state it without quoting. Do not state anything the results do not "
    "support. If results disagree, state the one with the higher authority, or the newer one when authority is "
    "equal, cite both, and add that another source says otherwise. Reply with JSON."
)

ANSWER_SCHEMA = {
    "type": "object",
    "properties": {"answer": {"type": "string"}},
    "required": ["answer"],
    "additionalProperties": False,
}

# The search template writes "Name in source on Mon D, YYYY: \"quote\"". That is a citation, not the answer.
_MONEY = re.compile(r"\$\d+(?:\.\d+)?[KMB]\b")
_CITATION = re.compile(
    r"^.+ in .+ on [A-Z][a-z]{2,8} \d{1,2}, \d{4}: \"(?P<quote>.*)\"$",
    re.DOTALL,
)


def _citation_quote(text: str) -> str | None:
    match = _CITATION.match(" ".join((text or "").split()))
    if match is None:
        return None
    quote = " ".join(match.group("quote").split())
    return quote or None


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
        ruled = self.fallback.route(question)
        if ruled.reason != "no rule matched":
            return ruled
        try:
            reply = self.llm.complete(
                [{"role": "system", "content": ROUTER_PROMPT}, {"role": "user", "content": question}],
                json_schema=ROUTE_SCHEMA,
            )
            return Route(Intent((reply.data or {})["intent"]), "llm")
        except Exception:
            return ruled


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
            claims = self.fallback.compose(state)
        else:
            try:
                known = TemplateComposer().compose(state)
            except Exception:
                known = []
            claims = [self._enrich(c, known) for c in claims]
        return self._stated_as_answer(state, claims)

    def _stated_as_answer(self, state: RunState, claims: list[Claim]) -> list[Claim]:
        """A search hit is stored as a citation. The sentence that replaces the prompt is the fact."""

        citations = [claim for claim in claims if _citation_quote(claim.text) is not None]
        if not citations:
            return claims
        sentence = self._answer_sentence(state, citations)
        if not sentence:
            sentence = " ".join(quote for quote in (_citation_quote(claim.text) for claim in citations) if quote)
        sentence = " ".join(sentence.split())
        if not sentence:
            return claims
        evidence = list(dict.fromkeys(item for claim in citations for item in claim.evidence_ids))
        rest = [claim for claim in claims if _citation_quote(claim.text) is None]
        return [Claim(sentence, evidence, "llm"), *rest]

    def _answer_sentence(self, state: RunState, citations: list[Claim]) -> str:
        notes = []
        for claim in citations:
            quote = _citation_quote(claim.text) or claim.text
            notes.append(f"{', '.join(claim.evidence_ids)}: {quote}")
        blank = blank_search(state.question) is not None
        if blank:
            system = (
                "The question is a blank in a sentence. Reply with only the missing value from the notes, "
                "such as $2.4M. Do not write a sentence. Do not name the author, the source, or the date. "
                "Reply with JSON."
            )
        else:
            system = (
                "Answer the question in one or two sentences using only these notes. "
                "State the fact the question asks for. Do not name the author, the source, or the date. "
                "Do not use quotation marks. Leave out labels that only say the note is a test. "
                "Reply with JSON."
            )
        try:
            reply = self.llm.complete(
                [
                    {"role": "system", "content": system},
                    {"role": "user", "content": state.question + "\n\n" + "\n".join(notes)},
                ],
                json_schema=ANSWER_SCHEMA,
            )
        except Exception:
            return ""
        text = str((reply.data or {}).get("answer") or "").strip()
        if not text or _citation_quote(text) is not None:
            return ""
        if blank:
            amounts = _MONEY.findall(text)
            if len(amounts) == 1:
                return amounts[0]
        return text

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


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"
DEFAULT_GROQ_MODEL = "openai/gpt-oss-120b"
_buckets: dict[int, "TokenBucket"] = {}
_buckets_lock = threading.Lock()


class TokenBucket:
    """Process-local bucket: ``rpm`` calls per minute, then wait for a token."""

    def __init__(self, rpm: int, *, clock=None, sleep=None):
        if rpm <= 0:
            raise ValueError("rpm must be positive")
        self.rpm = rpm
        self.capacity = float(rpm)
        self._interval = 60.0 / float(rpm)
        self._clock = clock or time.monotonic
        self._sleep = sleep or time.sleep
        self._lock = threading.Lock()
        self._tokens = self.capacity
        self._updated = self._clock()

    def acquire(self) -> None:
        """Take one token, waiting outside the lock while the bucket is empty."""

        while True:
            with self._lock:
                now = self._clock()
                elapsed = now - self._updated
                if elapsed < 0:
                    elapsed = 0.0
                self._updated = now
                self._tokens = min(self.capacity, self._tokens + elapsed / self._interval)
                if self._tokens >= 1.0:
                    self._tokens -= 1.0
                    return
                wait = (1.0 - self._tokens) * self._interval
            self._sleep(wait)


def _shared_bucket(rpm: int) -> TokenBucket:
    """One bucket per rate for every Groq client in this process."""

    with _buckets_lock:
        bucket = _buckets.get(rpm)
        if bucket is None:
            bucket = TokenBucket(rpm)
            _buckets[rpm] = bucket
        return bucket


class GroqLLM:
    """Groq's OpenAI-compatible chat API. The key is sent only as a bearer token."""

    def __init__(self, *, model: str, api_key: str, base_url: str = GROQ_URL, http=None,
                 env_name: str = "GROQ_API_KEY", rpm: int = 30, clock=None, sleep=None, own_bucket: bool = False):
        if not api_key:
            raise RuntimeError(f"{env_name} is not set")
        self.model = model or DEFAULT_GROQ_MODEL
        self.rpm = rpm
        self._api_key = api_key
        self._url = base_url or GROQ_URL
        self._http = http or _groq_http
        # A fake clock, or a company's own key, gets its own bucket so it does not drain the process limiter.
        own = own_bucket or clock or sleep
        self._bucket = TokenBucket(rpm, clock=clock, sleep=sleep) if own else _shared_bucket(rpm)

    def __repr__(self) -> str:
        return f"GroqLLM(model={self.model!r})"

    @classmethod
    def from_config(cls, cfg: WeaveConfig, *, clock=None, sleep=None) -> "GroqLLM":
        llm = cfg.answer.llm
        env_name = llm.api_key_env or "GROQ_API_KEY"
        return cls(model=llm.model or DEFAULT_GROQ_MODEL, api_key=os.environ.get(env_name, ""),
                   base_url=llm.base_url or GROQ_URL, env_name=env_name, rpm=llm.rpm,
                   clock=clock, sleep=sleep)

    def complete(self, messages, tools=None, json_schema=None) -> LLMReply:
        payload: dict = {"model": self.model, "messages": list(messages), "temperature": 0}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        if json_schema is not None:
            payload["response_format"] = {"type": "json_object"}
            payload["messages"] = [*payload["messages"], {
                "role": "system",
                "content": "Reply with JSON matching this schema: " + json.dumps(json_schema),
            }]
        body = json.dumps(payload).encode()
        if self._api_key.encode() in body:
            raise RuntimeError("refusing to place the Groq key in the request body")
        self._bucket.acquire()
        status, raw = self._http(self._url, {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "User-Agent": "weave-agent",
        }, body)
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"Groq returned unreadable JSON ({status})") from exc
        if status >= 400 or not isinstance(data, dict) or "error" in data and "choices" not in data:
            error = data.get("error") if isinstance(data, dict) else None
            message = error.get("message") if isinstance(error, dict) else None
            if not isinstance(message, str) or not message:
                message = "request failed"
            raise RuntimeError(f"Groq error ({status}): {message[:200]}")
        message = (data.get("choices") or [{}])[0].get("message") or {}
        calls = []
        for call in message.get("tool_calls") or []:
            function = call.get("function") or {}
            arguments = function.get("arguments") or {}
            if isinstance(arguments, str):
                try:
                    arguments = json.loads(arguments) if arguments else {}
                except json.JSONDecodeError:
                    arguments = {}
            if not isinstance(arguments, dict):
                arguments = {}
            calls.append(LLMToolCall(str(function.get("name") or ""), arguments))
        text = message.get("content") or ""
        if not isinstance(text, str):
            text = ""
        parsed = None
        if json_schema is not None and text.strip():
            parsed = json.loads(text)
        return LLMReply(text=text, tool_calls=calls, data=parsed)


def _groq_http(url: str, headers: dict, body: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


_TENANT: ContextVar[str | None] = ContextVar("weave_llm_tenant", default=None)
_SAVED_KEY_TTL = 300.0
_saved_keys: dict[str, tuple[float, str]] = {}
_saved_lock = threading.Lock()


class KeyRejected(RuntimeError):
    """Groq refused a company's own key. The caller is not moved onto the server key."""


@contextmanager
def using_tenant(tenant: str | None):
    """Model calls inside this block bill ``tenant``'s own Groq key when it saved one."""

    token = _TENANT.set(tenant)
    try:
        yield
    finally:
        _TENANT.reset(token)


def saved_key(tenant: str | None) -> str:
    """The Groq key a company saved on /setup, or ''. Cached for a few minutes per company."""

    if not tenant:
        return ""
    now = time.monotonic()
    with _saved_lock:
        hit = _saved_keys.get(tenant)
        if hit is not None and now - hit[0] < _SAVED_KEY_TTL:
            return hit[1]
    try:
        from app.connect.credentials import row

        found = row(tenant, "groq") or {}
        key = str(found.get("token") or "") if found.get("status") != "failed" else ""
    except Exception:
        key = ""
    with _saved_lock:
        _saved_keys[tenant] = (now, key)
    return key


def forget_saved_key(tenant: str) -> None:
    with _saved_lock:
        _saved_keys.pop(tenant, None)


class TenantKeyLLM:
    """The company's own Groq key when it saved one, the server key otherwise.

    The company comes from ``using_tenant``. A rejected company key marks the
    saved grant failed and raises ``KeyRejected``; it never falls back to the
    server key, so a company's usage is never moved onto the server bill.
    """

    def __init__(self, shared: GroqLLM, *, key_for: Callable[[str | None], str] = saved_key):
        self.shared = shared
        self.model = shared.model
        self._key_for = key_for
        self._clients: dict[str, GroqLLM] = {}
        self._lock = threading.Lock()

    def __repr__(self) -> str:
        return f"TenantKeyLLM(model={self.model!r})"

    def client_for(self, tenant: str | None) -> GroqLLM:
        key = self._key_for(tenant)
        if not key:
            return self.shared
        with self._lock:
            client = self._clients.get(key)
            if client is None:
                client = GroqLLM(model=self.shared.model, api_key=key, base_url=self.shared._url,
                                 http=self.shared._http, rpm=self.shared.rpm, own_bucket=True)
                self._clients[key] = client
        return client

    def complete(self, messages, tools=None, json_schema=None) -> LLMReply:
        tenant = _TENANT.get()
        client = self.client_for(tenant)
        try:
            return client.complete(messages, tools, json_schema)
        except RuntimeError as exc:
            if client is self.shared or "(401)" not in str(exc):
                raise
            _mark_key_failed(tenant)
            raise KeyRejected("the company's Groq key was rejected; save a new one on /setup") from exc


def _mark_key_failed(tenant: str | None) -> None:
    if not tenant:
        return
    forget_saved_key(tenant)
    try:
        from app.connect.credentials import current_store

        current_store().update_status(tenant, "groq", "failed")
    except Exception:
        pass


def build_components(cfg: WeaveConfig, llm: LLM | None = None):
    """Router, planner and composer for ``answer.llm.provider``; ``none`` is the rule-based set."""

    provider = cfg.answer.llm.provider
    if llm is None:
        if provider == "none":
            return RuleRouter(), RulePlanner(), TemplateComposer()
        if provider == "groq":
            llm = TenantKeyLLM(GroqLLM.from_config(cfg))
        else:
            raise NotImplementedError(f"no adapter for LLM provider {provider!r}; use groq or none")
        # The playbook picks the tools. The model writes the sentence; a citation-shaped
        # search hit is rewritten into the fact before it can be pasted into the prompt.
        return LLMRouter(llm), RulePlanner(), LLMComposer(llm, cfg)
    return LLMRouter(llm), LLMPlanner(llm, cfg), LLMComposer(llm, cfg)
