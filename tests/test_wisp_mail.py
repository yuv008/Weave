"""POST /wisp/mail offline: @bubble gate, scripted model, auth, and no episode writes.

The model is ``ScriptedLLM``, the same stand-in as the answer-agent tests, so
nothing here calls Groq. ``draft_email`` still runs for real and stores a proposal.
"""

import json
import logging
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

from agents.answer.agent import build_agent
from agents.answer.llm import LLMReply, LLMToolCall, ScriptedLLM
from agents.graph import ReadOnlyGraph
from agents.mcp.server import build_app
from agents.tools.base import Citation, ToolResult
from agents.tools.invoke import invoke as real_invoke
from tests.answer_support import SubjectGraph, offline_runtime

TOKEN = "tok-sam"
AUTH = {"authorization": f"Bearer {TOKEN}"}
MARKER = "MAIL_BODY_MARKER_9f3a"
FACT = "acme:figure:margin"
ITEM = "acme:gdrive:document:board:1"
SENTENCE = "The profit margin this quarter is 18%."
DRAFT_BODY = f"@bubble email the sales team this quarter's profit margin\n\n{MARKER}"
SENTENCE_BODY = f"{MARKER} our margin this quarter is @Bubble"
UNSURE_BODY = f"our margin this quarter is @bubble\n{MARKER}"
EMPTY = {
    "state": "unsure", "mode": "sentence", "summary": "", "quote": "", "source": "", "item_id": "", "insert": "",
}


class FactGraph(SubjectGraph):
    """Subject matching, plus the rows ``draft_email`` reads for one visible figure."""

    def ro_query(self, query, params=None):
        params = params or {}
        compact = " ".join(query.split())
        ids = set(params.get("ids") or [])
        if "quote" in compact:
            rows = [[
                FACT, ITEM, "Board pack: profit margin is 18 percent.", "CFO",
                "2026-10-01T00:00:00Z", "gdrive", None, None,
            ]] if FACT in ids else []
            return SimpleNamespace(result_set=rows)
        if "AS fid" in compact:
            rows = [[FACT, ["Figure"], None, "Profit margin", 18, "%", "2026-10-01T00:00:00Z"]] if FACT in ids else []
            return SimpleNamespace(result_set=rows)
        if "gate_evidence" in compact:
            rows = [[FACT]] if FACT in ids else []
            return SimpleNamespace(result_set=rows)
        if "SUMMARIZES" in compact or "count(i)" in compact:
            rows = [[0]] if "count(i)" in compact else []
            return SimpleNamespace(result_set=rows)
        return super().ro_query(query, params)


def _figure_result():
    return ToolResult(
        "get_figures",
        {"figures": [{
            "id": FACT, "name": "Profit margin", "value": 18, "unit": "%", "as_of": "2026-10-01T00:00:00Z",
            "evidence": [ITEM], "sources": ["gdrive"], "confidence": 0.9, "likely": False, "stale": False,
            "history": [],
        }]},
        node_ids=[FACT, ITEM],
        citations=[Citation(ITEM, "Board pack: profit margin is 18 percent.", "CFO", "2026-10-01T00:00:00Z", "gdrive", None)],
    )


def _install_tools(monkeypatch, figures):
    def invoke(name, ctx, args, cache=None):
        if name == "get_figures":
            return figures
        if name == "search_context":
            return ToolResult("search_context", {"hits": []})
        return real_invoke(name, ctx, args, cache)

    monkeypatch.setattr("agents.answer.loop.invoke", invoke)


def _agent(env, llm):
    return build_agent(env.runtime, llm=llm)


def _client(env, agent):
    return TestClient(build_app(env.runtime, agent=agent), base_url="http://127.0.0.1:8765")


def _post(client, body, headers=AUTH):
    return client.post("/wisp/mail", json={"to": "sales team", "subject": "Q3", "body": body}, headers=headers)


def _offered(llm, index=0):
    return {tool["function"]["name"] for tool in llm.requests[index]["tools"]}


def _size(env):
    memory = env.runtime.memory
    turns = sum(len(rows) for rows in memory._turns.values())
    return (len(memory.episodes), len(memory.conversations), turns, len(memory.tasks), len(env.runtime.audit.rows))


def _stored(env) -> str:
    memory = env.runtime.memory
    parts = []
    for episode in memory.episodes.values():
        parts.extend([episode.question or "", episode.note or ""])
    for turns in memory._turns.values():
        parts.extend(turn.text for turn in turns)
    for task in memory.tasks.values():
        parts.append(json.dumps(task.payload, default=str))
    for row in env.runtime.audit.rows:
        parts.append(json.dumps(row, default=str))
    return "\n".join(parts)


@pytest.fixture()
def env():
    env = offline_runtime()
    env.store.add_token(TOKEN, env.fx.ids["sam"])
    return env


def test_missing_bubble_does_not_call_the_model(env):
    llm = ScriptedLLM([])
    before = _size(env)
    with _client(env, _agent(env, llm)) as client:
        responses = [
            _post(client, "email the sales team this quarter's profit margin"),
            _post(client, f"{MARKER} our margin this quarter is strong"),
            _post(client, "bubble, please write the update"),
        ]
    assert llm.requests == []
    for response in responses:
        assert response.status_code == 200
        assert response.json() == EMPTY
    assert _size(env) == before
    assert MARKER not in _stored(env)


def test_bubble_instruction_returns_a_draft(env, monkeypatch):
    _install_tools(monkeypatch, _figure_result())
    env.runtime.open_graph = lambda tenant: ReadOnlyGraph(FactGraph(env.fx), f"co:{tenant}")
    llm = ScriptedLLM([
        LLMReply(tool_calls=[LLMToolCall("get_figures", {"subject": "profit margin"})]),
        LLMReply(tool_calls=[LLMToolCall("draft_email", {
            "intent": "this quarter's profit margin", "recipient": "sales team", "fact_ids": [FACT],
        })]),
        LLMReply(text="done"),
    ])
    before = _size(env)
    with _client(env, _agent(env, llm)) as client:
        response = _post(client, DRAFT_BODY)
    assert response.status_code == 200
    body = response.json()
    tasks = list(env.runtime.memory.tasks.values())
    assert len(tasks) == 1 and tasks[0].kind == "proposal_email" and tasks[0].state == "proposed"
    draft = tasks[0].payload["draft"]
    assert set(body) == {"state", "mode", "summary", "quote", "source", "item_id", "insert"}
    assert body["state"] == "found" and body["mode"] == "draft"
    assert body["insert"] == draft and body["insert"]
    assert "Profit margin: 18 %." in body["insert"]
    assert "@bubble" not in body["insert"].lower() and MARKER not in body["insert"]
    assert body["item_id"] == FACT
    assert body["source"] == "gdrive"
    assert "18 percent" in body["quote"]
    assert body["summary"] == "Draft from gdrive"
    assert _offered(llm) == {"draft_email", "get_figures", "search_context"}
    assert _size(env) == (before[0], before[1], before[2], before[3] + 1, before[4])
    assert MARKER not in _stored(env)


def test_draft_uses_a_passage_when_the_notes_are_not_figures(env, monkeypatch):
    quote = "Weave test doc. A small test document for the Weave Google Drive connector."

    def invoke(name, ctx, args, cache=None):
        if name == "search_context":
            return ToolResult(
                "search_context",
                {"hits": [{"item_id": ITEM, "snippet": quote, "source": "gdrive"}]},
                node_ids=[ITEM],
                citations=[Citation(ITEM, quote, None, None, "gdrive", None)],
            )
        if name == "draft_email":
            return ToolResult("draft_email", {}, message="None of those facts are visible to you, so there is nothing to draft.")
        return ToolResult(name, {})

    monkeypatch.setattr("agents.answer.loop.invoke", invoke)
    llm = ScriptedLLM([
        LLMReply(tool_calls=[LLMToolCall("search_context", {"query": "Weave small document"})]),
        LLMReply(tool_calls=[LLMToolCall("draft_email", {
            "intent": "what the document is", "recipient": "team", "fact_ids": [ITEM],
        })]),
        LLMReply(text="done"),
    ])
    before = _size(env)
    with _client(env, _agent(env, llm)) as client:
        response = _post(client, "@bubble email the team what the Weave small document is")
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "found" and body["mode"] == "draft"
    assert body["item_id"] == ITEM and body["source"] == "gdrive"
    assert body["summary"] == "This came up in gdrive"
    assert quote in body["insert"] and quote in body["quote"]
    assert "@bubble" not in body["insert"].lower()
    assert _size(env) == before
    assert MARKER not in _stored(env)


def test_bubble_placeholder_returns_a_sentence(env, monkeypatch):
    _install_tools(monkeypatch, _figure_result())
    llm = ScriptedLLM([
        LLMReply(tool_calls=[LLMToolCall("get_figures", {"subject": "profit margin"})]),
        LLMReply(text="done"),
        LLMReply(data={"claims": [{"text": SENTENCE, "evidence_ids": [ITEM]}]}),
    ])
    before = _size(env)
    with _client(env, _agent(env, llm)) as client:
        response = _post(client, SENTENCE_BODY)
    assert response.status_code == 200
    body = response.json()
    assert body == {
        "state": "found",
        "mode": "sentence",
        "summary": "Profit margin from gdrive",
        "quote": "Board pack: profit margin is 18 percent.",
        "source": "gdrive",
        "item_id": FACT,
        "insert": SENTENCE,
    }
    assert _offered(llm) == {"get_figures", "search_context"}
    assert "draft_email" not in _offered(llm)
    assert _size(env) == before
    assert MARKER not in _stored(env)
    assert MARKER in json.dumps(llm.requests)


def test_unsure_when_no_visible_fact(env, monkeypatch):
    _install_tools(monkeypatch, ToolResult("get_figures", {"figures": []}, message="No figures on that are visible to you."))
    llm = ScriptedLLM([
        LLMReply(tool_calls=[LLMToolCall("get_figures", {"subject": "profit margin"})]),
        LLMReply(text="done"),
        LLMReply(data={"claims": [{"text": "The profit margin is 42%.", "evidence_ids": ["acme:figure:made-up"]}]}),
    ])
    before = _size(env)
    with _client(env, _agent(env, llm)) as client:
        response = _post(client, UNSURE_BODY)
    assert response.status_code == 200
    assert response.json() == EMPTY
    assert llm.requests
    assert _size(env) == before
    assert MARKER not in _stored(env)


def test_missing_bearer_token_matches_cue(env):
    llm = ScriptedLLM([])
    before = _size(env)
    with _client(env, _agent(env, llm)) as client:
        mail = client.post("/wisp/mail", json={"to": "sales team", "subject": "Q3", "body": DRAFT_BODY})
        cue = client.post("/wisp/cue", json={"prompt": "enterprise SSO"})
    assert mail.status_code == cue.status_code == 401
    assert mail.text == cue.text == "not authorized"
    assert mail.headers["www-authenticate"] == cue.headers["www-authenticate"] == "Bearer"
    assert llm.requests == []
    assert _size(env) == before


def test_empty_and_overlong_body_are_rejected(env):
    llm = ScriptedLLM([])
    before = _size(env)
    headers = {**AUTH, "content-type": "application/json"}
    with _client(env, _agent(env, llm)) as client:
        rejected = [
            client.post("/wisp/mail", content=b"", headers=headers),
            client.post("/wisp/mail", content=b"null", headers=headers),
            client.post("/wisp/mail", content=b"[]", headers=headers),
            client.post("/wisp/mail", content=b'"hello"', headers=headers),
            client.post("/wisp/mail", json={"to": "sales team", "subject": "Q3"}, headers=AUTH),
            client.post("/wisp/mail", json={"to": "sales team", "subject": "Q3", "body": ""}, headers=AUTH),
            client.post("/wisp/mail", json={"to": "sales team", "subject": "Q3", "body": "   "}, headers=AUTH),
            client.post("/wisp/mail", json={"to": 1, "subject": "Q3", "body": "@bubble"}, headers=AUTH),
            client.post("/wisp/mail", json={"to": "sales team", "subject": "Q3", "body": "a" * 8001}, headers=AUTH),
            client.post(
                "/wisp/mail",
                json={"to": "sales team", "subject": "Q3", "body": f"@bubble email the team {'x' * 8000}"},
                headers=AUTH,
            ),
        ]
        accepted = _post(client, "z" * 8000)
    for response in rejected:
        assert response.status_code == 400
    assert accepted.status_code == 200 and accepted.json() == EMPTY
    assert llm.requests == []
    assert _size(env) == before


def test_mail_does_not_store_the_body_as_an_episode(env, monkeypatch, caplog):
    _install_tools(monkeypatch, _figure_result())
    env.runtime.open_graph = lambda tenant: ReadOnlyGraph(FactGraph(env.fx), f"co:{tenant}")
    llm = ScriptedLLM([
        LLMReply(tool_calls=[LLMToolCall("get_figures", {"subject": "profit margin"})]),
        LLMReply(tool_calls=[LLMToolCall("draft_email", {
            "intent": "this quarter's profit margin", "recipient": "sales team", "fact_ids": [FACT],
        })]),
        LLMReply(text="done"),
        LLMReply(tool_calls=[LLMToolCall("get_figures", {"subject": "profit margin"})]),
        LLMReply(text="done"),
        LLMReply(data={"claims": [{"text": SENTENCE, "evidence_ids": [ITEM]}]}),
    ])
    before = _size(env)
    caplog.set_level(logging.DEBUG)
    with _client(env, _agent(env, llm)) as client:
        quiet = _post(client, f"hello {MARKER}")
        drafted = _post(client, DRAFT_BODY)
        filled = _post(client, SENTENCE_BODY)
    assert quiet.status_code == drafted.status_code == filled.status_code == 200
    assert quiet.json()["state"] == "unsure" and drafted.json()["state"] == "found"
    assert filled.json()["insert"] == SENTENCE
    assert _size(env)[:3] == before[:3]
    assert len(env.runtime.memory.tasks) == before[3] + 1
    assert len(env.runtime.audit.rows) == before[4]
    assert MARKER not in _stored(env)
    assert MARKER not in caplog.text
    episodes = list(env.runtime.memory.episodes.values())
    assert all(MARKER not in (episode.question or "") and MARKER not in (episode.note or "") for episode in episodes)


FILL = "hey, as per discussed the profit is @bubble it has grown from @bubble"
ASK = "@bubble can you draft email about the budget and sales"
PROFIT = "real:slack:message:demo:profit-current"
EARLIER = "real:slack:message:demo:profit-prior"
BUDGET = "real:slack:message:demo:budget"
SALES = "real:slack:message:demo:sales"


def test_two_blanks_on_one_line_fill_different_figures(env, monkeypatch):
    from agents.wisp.mail import bubble_blanks, bubble_mode

    def invoke(name, ctx, args, cache=None):
        assert name == "search_context"
        query = args.get("query") or ""
        if query == "grown":
            hit = {"item_id": EARLIER, "snippet": "The earlier amount grown from is $1.8M.", "source": "slack"}
        else:
            assert query == "discussed profit"
            hit = {"item_id": PROFIT, "snippet": "The current profit discussed is $2.4M.", "source": "slack"}
        return ToolResult(
            "search_context",
            {"hits": [hit]},
            node_ids=[hit["item_id"]],
            citations=[Citation(hit["item_id"], hit["snippet"], None, None, "slack", None)],
        )

    monkeypatch.setattr("agents.wisp.mail.invoke", invoke)
    llm = ScriptedLLM([])
    before = _size(env)
    assert bubble_mode(FILL) == "sentence"
    assert bubble_blanks(FILL) == [
        "hey, as per discussed the profit is",
        "it has grown from",
    ]
    with _client(env, _agent(env, llm)) as client:
        response = _post(client, FILL)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "found" and body["mode"] == "draft"
    assert body["insert"] == "hey, as per discussed the profit is $2.4M it has grown from $1.8M"
    assert body["item_id"] == PROFIT
    assert "$2.4M" in body["insert"] and "$1.8M" in body["insert"]
    assert "@bubble" not in body["insert"].lower()
    assert llm.requests == []
    assert _size(env) == before


def test_budget_and_sales_request_becomes_a_short_email(env, monkeypatch):
    from agents.wisp.mail import bubble_mode

    def invoke(name, ctx, args, cache=None):
        assert name == "search_context"
        assert args.get("query") == "budget sales"
        hits = [
            {"item_id": BUDGET, "snippet": "The budget is $900K.", "source": "slack"},
            {"item_id": SALES, "snippet": "The sales are $3.1M.", "source": "slack"},
        ]
        return ToolResult("search_context", {"hits": hits}, node_ids=[BUDGET, SALES])

    monkeypatch.setattr("agents.wisp.mail.invoke", invoke)
    llm = ScriptedLLM([])
    before = _size(env)
    assert bubble_mode(ASK) == "draft"
    with _client(env, _agent(env, llm)) as client:
        response = _post(client, ASK)
    assert response.status_code == 200
    body = response.json()
    assert body["state"] == "found" and body["mode"] == "draft"
    assert body["insert"] == "\n".join([
        "Hi,",
        "",
        "The budget is $900K. The sales are $3.1M.",
        "",
        "Best regards,",
    ])
    assert body["item_id"] == BUDGET
    assert llm.requests == []
    assert _size(env) == before
