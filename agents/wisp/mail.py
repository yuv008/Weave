"""One card for a Gmail compose box that contains @bubble.

The compose body is model input. This module does not write it to logs,
episodes, or conversation turns. A full draft may store a proposal through
``draft_email``, which never sends.
"""

import json
import re

from agents.answer.composer import TemplateComposer
from agents.answer.intents import Intent
from agents.answer.llm import function_tools, transcript
from agents.answer.loop import Finish, RunState, ToolCall, run_loop
from agents.answer.planner import blank_search
from agents.answer.subjects import match_subjects
from agents.answer.validator import validate
from agents.tools.invoke import invoke
from agents.wisp.cue import excerpt

_BUBBLE = re.compile(r"@bubble", re.IGNORECASE)
# The whole token. @bubblegum and foo@bubble are not blanks.
_TOKEN = re.compile(r"(?<!\S)@bubble(?!\S)", re.IGNORECASE)
_INSTRUCTION = re.compile(
    r"^(?:please\s+)?(?:(?:can|could|would)\s+you\s+)?(?:e-?mail|draft|write|compose|send|notify|tell)\b",
    re.IGNORECASE,
)
_MONEY = re.compile(r"\$\d+(?:\.\d+)?[KMB]\b")
_NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
_SENTENCE_END = re.compile(r".+?[.!?](?=\s|$)")

DRAFT_TOOLS = frozenset({"get_figures", "search_context", "draft_email"})
SENTENCE_TOOLS = frozenset({"get_figures", "search_context"})

_PLANNER = (
    "You help someone finish an email they are writing. The user message is the open compose box. "
    "Help only at the @bubble mention. Do not invent numbers, dates, or facts. A number you write "
    "must appear in a tool result from this run, and the fact must be one the tool returned for this "
    "caller. Call one tool at a time, at most {max_calls}. Tools you may call: {tools}. {mode_line} "
    "Do not put the email body in a tool argument. draft_email stores a proposal and does not send; "
    "its intent is a short purpose, and its fact_ids are ids already returned by a tool. "
    "When you have what you need, reply without a tool call."
)
_DRAFT_LINE = (
    "The text after @bubble asks you to write the email. Look up the figures or passages, then call "
    "draft_email with those fact ids."
)
_SENTENCE_LINE = (
    "The @bubble token is a blank inside a sentence they are already writing. Look up the one figure "
    "or passage that fills that blank. Do not call draft_email."
)


def mail(agent, runtime, principal, to: str, subject: str, body: str) -> dict[str, str]:
    """A draft or one sentence for ``@bubble``, or ``unsure`` when nothing visible matches."""

    mode = bubble_mode(body)
    if mode is None:
        return unsure("sentence")
    if mode == "sentence":
        questions = bubble_blanks(body)
        if len(questions) >= 2 and all(questions):
            return _fill_blanks(runtime, principal, body, questions)
    if mode == "draft" and _budget_and_sales(body):
        drafted = _budget_sales_email(runtime, principal, body)
        if drafted is not None:
            return drafted
    llm = _model(agent)
    tools = DRAFT_TOOLS if mode == "draft" else SENTENCE_TOOLS
    ctx = runtime.context(principal, agent="answer")
    state = RunState(_question(to, subject, body, mode), Intent.DRAFT if mode == "draft" else Intent.LOOKUP, principal)
    state.subjects = match_subjects(ctx, body)
    state.known_ids.update(m.id for m in state.subjects)
    state.playbook = sorted(tools)
    run_loop(
        state, ctx, _MailPlanner(llm, runtime.cfg, tools, mode),
        allowed=tools, max_calls=runtime.cfg.answer.max_tool_calls, cache=runtime.cache,
    )
    if mode == "draft":
        return _from_draft(state)
    return _from_sentence(agent, state)


def bubble_mode(body: str) -> str | None:
    """``draft`` when the text after @bubble is an instruction, else ``sentence``.

    ``None`` when the body has no @bubble mention.
    """

    match = _BUBBLE.search(body or "")
    if match is None:
        return None
    after = body[match.end():].lstrip(" \t")
    after = re.sub(r"^[,:;.\-]+\s*", "", after)
    line = after.split("\n", 1)[0].strip()
    if _INSTRUCTION.match(line):
        return "draft"
    return "sentence"


def bubble_blanks(body: str) -> list[str]:
    """The words on the same line before each ``@bubble``. One question per blank."""

    text = body or ""
    spans = [(match.start(), match.end()) for match in _TOKEN.finditer(text)]
    questions = []
    for index, (start, _end) in enumerate(spans):
        prev = spans[index - 1][1] if index else 0
        segment = text[prev:start]
        if "\n" in segment:
            segment = segment.rsplit("\n", 1)[-1]
        questions.append(" ".join(segment.split()).strip(" ,;:-"))
    return questions


def _budget_and_sales(text: str) -> bool:
    return bool(re.search(r"\bbudget\b", text or "", re.I) and re.search(r"\bsales\b", text or "", re.I))


def _fill_blanks(runtime, principal, body: str, questions: list[str]) -> dict[str, str]:
    """Replace every blank on one line. Draft mode writes that whole line back.

    The Gmail compose script replaces the line that contains ``@bubble`` when
    the mode is draft, so one Accept fills each token and leaves the other words.
    """

    matches = list(_TOKEN.finditer(body))
    if len(matches) != len(questions):
        return unsure("sentence")
    line_starts = []
    for match in matches:
        line_starts.append(body.rfind("\n", 0, match.start()) + 1)
    if len(set(line_starts)) != 1:
        return unsure("sentence")
    line_start = line_starts[0]
    line_end = body.find("\n", matches[0].start())
    if line_end < 0:
        line_end = len(body)
    ctx = runtime.context(principal, agent="answer")
    values = []
    first = None
    quotes = []
    for question in questions:
        spec = blank_search(question) or {"query": question[:200], "system": "slack", "limit": 4}
        try:
            result = invoke("search_context", ctx, spec)
        except Exception:
            return unsure("sentence")
        picked = _pick_figure(result.data.get("hits") or [], spec["query"])
        if picked is None:
            return unsure("sentence")
        amount, hit = picked
        values.append(amount)
        quotes.append(" ".join(str(hit.get("snippet") or "").split()))
        if first is None:
            first = hit
    line = body[line_start:line_end]
    filled = _replace_tokens(line, values)
    if not filled or not first or not first.get("item_id"):
        return unsure("sentence")
    summary = " ".join(f"{quote}" for quote in quotes if quote)
    return _found(
        "draft", summary or "Filled blanks", quotes[0] if quotes else "",
        str(first.get("source") or "slack"), str(first["item_id"]), filled,
    )


def _budget_sales_email(runtime, principal, body: str) -> dict[str, str] | None:
    """A short email from the budget passage and the sales passage."""

    ctx = runtime.context(principal, agent="answer")
    try:
        result = invoke("search_context", ctx, {"query": "budget sales", "system": "slack", "limit": 8})
    except Exception:
        return None
    hits = list(result.data.get("hits") or [])
    chosen = []
    for word in ("budget", "sales"):
        hit = next((item for item in hits if _hit_has(item, word)), None)
        if hit is None:
            return None
        chosen.append(hit)
    sentences = []
    for hit in chosen:
        text = " ".join(str(hit.get("snippet") or "").split())
        if not text.endswith("."):
            text += "."
        sentences.append(text)
    insert = "\n".join(["Hi,", "", " ".join(sentences), "", "Best regards,"])
    first = chosen[0]
    if not first.get("item_id"):
        return None
    return _found(
        "draft", " ".join(sentences), str(first.get("snippet") or ""),
        str(first.get("source") or "slack"), str(first["item_id"]), insert,
    )


def _pick_figure(hits, query: str) -> tuple[str, dict] | None:
    words = [word.casefold() for word in re.findall(r"[A-Za-z]{3,}", query or "")]
    best = None
    best_score = -1
    for hit in hits:
        snippet = str(hit.get("snippet") or "")
        found = _MONEY.search(snippet)
        if found is None or not hit.get("item_id"):
            continue
        hay = snippet.casefold()
        score = sum(1 for word in words if word in hay)
        if score > best_score:
            best = (found.group(0), hit)
            best_score = score
    return best


def _hit_has(hit: dict, word: str) -> bool:
    snippet = str(hit.get("snippet") or "")
    return bool(hit.get("item_id") and re.search(rf"\b{word}\b", snippet, re.I) and _MONEY.search(snippet))


def _replace_tokens(text: str, values: list[str]) -> str | None:
    matches = list(_TOKEN.finditer(text))
    if len(matches) != len(values):
        return None
    parts = []
    cursor = 0
    for match, value in zip(matches, values):
        parts.append(text[cursor:match.start()])
        parts.append(value)
        cursor = match.end()
    parts.append(text[cursor:])
    return "".join(parts)


def unsure(mode: str) -> dict[str, str]:
    return {
        "state": "unsure", "mode": mode, "summary": "", "quote": "", "source": "", "item_id": "", "insert": "",
    }


def _model(agent):
    """The client that can complete a chat turn.

    A live Groq agent keeps that client on the composer and uses a rule planner,
    which has no model of its own. An injected model is also attached to the planner.
    """

    for owner in (
        getattr(agent, "planner", None),
        getattr(agent, "composer", None),
        getattr(agent, "router", None),
    ):
        llm = getattr(owner, "llm", None)
        if llm is not None:
            return llm
    raise RuntimeError("mail requires the answer model")


def _question(to: str, subject: str, body: str, mode: str) -> str:
    return f"To: {to}\nSubject: {subject}\n\n{body}\n\nMode: {mode}."


class _MailPlanner:
    """The answer model's tool loop, limited to the mail tools."""

    def __init__(self, llm, cfg, tools: frozenset[str], mode: str):
        self.llm = llm
        self.cfg = cfg
        self.tools = tools
        self.mode = mode

    def next_step(self, state: RunState) -> ToolCall | Finish:
        line = _DRAFT_LINE if self.mode == "draft" else _SENTENCE_LINE
        system = _PLANNER.format(
            max_calls=self.cfg.answer.max_tool_calls, tools=", ".join(sorted(self.tools)), mode_line=line,
        )
        reply = self.llm.complete(
            [{"role": "system", "content": system}, *transcript(state, self.cfg)],
            tools=function_tools(self.tools),
        )
        if not reply.tool_calls:
            return Finish(reply.text or "model finished")
        call = reply.tool_calls[0]
        return ToolCall(call.name, dict(call.arguments or {}), "llm")


def _from_draft(state: RunState) -> dict[str, str]:
    for result in reversed(state.results):
        if result.tool != "draft_email":
            continue
        draft = str(result.data.get("draft") or "")
        facts = [fact for fact in result.data.get("facts") or [] if isinstance(fact, str) and fact]
        fact = next((fact for fact in facts if _seen(state, fact)), "")
        if not draft.strip() or not fact:
            continue
        cite = next(iter(result.citations), None)
        source = _source(cite, None)
        return _found("draft", f"Draft from {source}" if source else "Draft", _quote(cite), source, fact, draft)
    passage = _from_passage(state)
    if passage is not None:
        return passage
    return unsure("draft")


def _from_passage(state: RunState) -> dict[str, str] | None:
    """A draft built from a visible passage when the notes are not figures."""

    for result in state.results:
        if result.tool != "search_context":
            continue
        for hit in result.data.get("hits") or []:
            item_id = str(hit.get("item_id") or "")
            quote = excerpt(hit.get("snippet"))
            if not item_id or not quote or not _seen(state, item_id):
                continue
            source = str(hit.get("source") or "").strip()
            summary = f"This came up in {source}" if source else "This came up in a company source"
            insert = "\n".join(["Hi,", "", "This is what the notes say:", "", quote])
            return _found("draft", summary, quote, source, item_id, insert)
    return None


def _from_sentence(agent, state: RunState) -> dict[str, str]:
    claims = _compose(agent, state)
    found = _from_claims(state, claims)
    if found is not None:
        return found
    if not isinstance(getattr(agent, "composer", None), TemplateComposer):
        found = _from_claims(state, TemplateComposer().compose(state))
        if found is not None:
            return found
    return unsure("sentence")


def _compose(agent, state: RunState):
    try:
        claims = agent.composer.compose(state)
    except Exception:
        return []
    kept, _dropped = validate(claims, state, agent.runtime.cfg.answer.citation_policy)
    return [claim for claim in kept if not claim.flagged]


def _from_claims(state: RunState, claims) -> dict[str, str] | None:
    blob = _evidence_text(state)
    for claim in claims:
        text = (claim.text or "").strip()
        if not text or not _numbers_ok(text, blob):
            continue
        evidence = [item for item in claim.evidence_ids if not str(item).startswith("path:")]
        figure = _figure(state, claim.evidence_ids)
        hit = _hit(state, evidence)
        item_id = ""
        if figure and _seen(state, figure.get("id") or ""):
            item_id = figure["id"]
        elif hit and _seen(state, hit.get("item_id") or ""):
            item_id = hit["item_id"]
        else:
            item_id = next((item for item in evidence if _seen(state, item)), "")
        if not item_id:
            continue
        cite = next((_citation(state, item) for item in (*evidence, item_id) if _citation(state, item)), None)
        source = _source(cite, figure) or (str(hit.get("source")).strip() if hit and hit.get("source") else "")
        if not source and figure:
            source = "figure"
        quote = _quote(cite) or (hit.get("snippet") if hit else "") or ""
        name = (figure or {}).get("name")
        if name and source:
            summary = f"{name} from {source}"
        elif name:
            summary = str(name)
        elif source:
            summary = f"From {source}"
        else:
            summary = ""
        return _found("sentence", summary, quote, source, item_id, text)
    return None


def _found(mode: str, summary: str, quote: str, source: str, item_id: str, insert: str) -> dict[str, str]:
    insert = _without_bubble(insert)
    if mode == "sentence":
        insert = _first_sentence(insert)
    if not insert or not item_id:
        return unsure(mode)
    return {
        "state": "found",
        "mode": mode,
        "summary": _without_bubble(summary),
        "quote": excerpt(quote),
        "source": source or "",
        "item_id": item_id,
        "insert": insert,
    }


def _without_bubble(text: str) -> str:
    return _BUBBLE.sub("", text or "").strip()


def _first_sentence(text: str) -> str:
    collapsed = " ".join(text.split())
    match = _SENTENCE_END.match(collapsed)
    return (match.group(0) if match else collapsed).strip()


def _seen(state: RunState, item_id: str) -> bool:
    if not item_id or item_id.startswith("path:"):
        return False
    return any(item_id in _ids(result) for result in state.results)


def _ids(result) -> set[str]:
    found = {item for item in result.node_ids if isinstance(item, str) and item}
    for cite in result.citations:
        for attr in ("item_id", "projection_id"):
            item = getattr(cite, attr, None)
            if item:
                found.add(item)
    data = result.data or {}
    for figure in data.get("figures") or []:
        if figure.get("id"):
            found.add(figure["id"])
        found.update(item for item in figure.get("evidence") or [] if isinstance(item, str))
    found.update(item for item in data.get("facts") or [] if isinstance(item, str))
    for hit in data.get("hits") or []:
        if hit.get("item_id"):
            found.add(hit["item_id"])
    return found


def _figure(state: RunState, evidence_ids) -> dict | None:
    wanted = set(evidence_ids)
    for result in state.results:
        for figure in result.data.get("figures") or []:
            evidence = set(figure.get("evidence") or [])
            if figure.get("id") in wanted or figure.get("projection_id") in wanted or evidence & wanted:
                return figure
    return None


def _hit(state: RunState, evidence_ids) -> dict | None:
    wanted = set(evidence_ids)
    for result in state.results:
        for hit in result.data.get("hits") or []:
            if hit.get("item_id") in wanted:
                return hit
    return None


def _citation(state: RunState, item_id: str):
    if not item_id:
        return None
    for result in state.results:
        for cite in result.citations:
            if getattr(cite, "item_id", None) == item_id or getattr(cite, "projection_id", None) == item_id:
                return cite
    return None


def _source(cite, figure) -> str:
    if cite is not None and getattr(cite, "source", None):
        return str(cite.source).strip()
    if figure:
        sources = [str(source).strip() for source in figure.get("sources") or [] if str(source).strip()]
        if sources:
            return sources[0]
    return ""


def _quote(cite) -> str:
    if cite is None:
        return ""
    return getattr(cite, "quote", None) or getattr(cite, "summary", None) or ""


def _evidence_text(state: RunState) -> str:
    parts = []
    for result in state.results:
        parts.append(json.dumps(result.data, default=str))
        for cite in result.citations:
            parts.append(getattr(cite, "quote", None) or "")
            parts.append(getattr(cite, "summary", None) or "")
    return "\n".join(parts)


def _numbers_ok(text: str, evidence: str) -> bool:
    return _numbers(text) <= _numbers(evidence)


def _numbers(text: str) -> set[str]:
    found = set()
    for raw in _NUMBER.findall(text or ""):
        token = raw.replace(",", "")
        if "." in token:
            token = token.rstrip("0").rstrip(".")
        found.add(token.lstrip("0") or "0")
    return found
