"""The answer agent offline: routing, guard rails, the citation validator, the card and the LLM path."""

from pathlib import Path

import json

import pytest
import yaml
from starlette.testclient import TestClient

from agents.answer import loop as loop_module
from agents.answer.agent import AnswerAgent, build_agent
from agents.answer.composer import Claim, TemplateComposer
from agents.answer.intents import ALLOWED, Intent, RuleRouter
from agents.answer.llm import LLMComposer, LLMReply, LLMRouter, LLMToolCall, ScriptedLLM, build_components
from agents.answer.loop import Finish, RunState, ToolCall, run_loop
from agents.answer.planner import DEFAULT_PLAYBOOKS, RulePlanner, blank_search, fact_ids, playbook_for
from agents.answer.subjects import match_subjects
from agents.answer.validator import validate
from agents.gate.principal import Surface
from agents.memory.recall import recall
from agents.mcp.server import build_app
from agents.tools.base import Citation, PathEdge, RestrictedCitation, ToolResult
from scripts.seed_playbooks import seed
from shared.config import load_config
from tests.answer_support import offline_runtime, principal

QUESTIONS = yaml.safe_load((Path(__file__).parent / "data" / "questions.yaml").read_text(encoding="utf-8"))
ITEM = "acme:slack:message:C0ENGSSO:1"
PROJ = "acme:projection:p1"
DECISION = "acme:decision:d1"


@pytest.fixture()
def env():
    env = offline_runtime()
    seed(env.runtime.memory)
    return env


class Script:
    """A planner that plays a fixed list of calls."""

    def __init__(self, calls):
        self.calls = list(calls)

    def next_step(self, state):
        return self.calls.pop(0) if self.calls else Finish()


def fake_invoke(results: dict):
    def invoke(name, ctx, args, cache=None):
        return results.get(name, ToolResult(name, {}))
    return invoke


def state_for(env, who, question, intent):
    p = principal(env, who)
    ctx = env.runtime.context(p)
    state = RunState(question, intent, p, subjects=match_subjects(ctx, question))
    state.known_ids.update(m.id for m in state.subjects)
    return ctx, state


def test_an_api_key_question_stays_a_lookup():
    route = RuleRouter().route("rn we are using the open ai api key can you change it to")
    assert route.intent == Intent.LOOKUP
    assert route.reason != "no rule matched"


def test_email_blanks_stay_lookups_and_a_budget_draft_searches():
    profit = RuleRouter().route("hey, as per discussed the profit is")
    earlier = RuleRouter().route("it has grown from")
    draft = RuleRouter().route("can you draft email about the budget and sales")
    assert profit.intent == earlier.intent == Intent.LOOKUP
    assert profit.reason != "no rule matched" and earlier.reason != "no rule matched"
    assert draft.intent == Intent.DRAFT
    assert blank_search("hey, as per discussed the profit is") == {
        "query": "discussed profit", "system": "slack", "limit": 4,
    }
    assert blank_search("it has grown from") == {"query": "grown", "system": "slack", "limit": 4}
    assert blank_search("can you draft email about the budget and sales") is None
    assert DEFAULT_PLAYBOOKS[Intent.DRAFT][-2:] == ["search_figures", "propose"]


def test_budget_and_sales_draft_uses_the_slack_figures(env):
    state = RunState("can you draft email about the budget and sales", Intent.DRAFT, principal(env, "sam"))
    state.subjects = []
    state.playbook = ["search_figures", "propose"]
    planner = RulePlanner()
    search = planner.next_step(state)
    assert isinstance(search, ToolCall)
    assert search.tool == "search_context"
    assert search.args == {"query": "budget sales", "system": "slack", "limit": 8}
    budget = "real:slack:message:demo:budget"
    sales = "real:slack:message:demo:sales"
    state.results.append(ToolResult("search_context", {"hits": [
        {"item_id": budget, "snippet": "The budget is $900K."},
        {"item_id": sales, "snippet": "The sales are $3.1M."},
    ]}, node_ids=[budget, sales]))
    state.known_ids.update([budget, sales])
    proposed = planner.next_step(state)
    assert isinstance(proposed, ToolCall) and proposed.tool == "draft_email"
    assert proposed.args["fact_ids"] == [budget, sales]
    assert fact_ids(state) == [budget, sales]
    skipped = RunState("email the sales team this quarter's profit margin", Intent.DRAFT, state.principal)
    skipped.subjects = []
    skipped.playbook = ["search_figures", "propose"]
    assert isinstance(RulePlanner().next_step(skipped), Finish)


def test_the_question_set_has_fifteen_questions_over_every_intent():
    assert len(QUESTIONS) == 15
    assert {q["intent"] for q in QUESTIONS} == {str(i) for i in Intent}


@pytest.mark.parametrize("case", QUESTIONS, ids=[q["question"][:40] for q in QUESTIONS])
def test_routing_picks_the_intent_and_first_tool(env, case):
    route = RuleRouter().route(case["question"])
    assert route.intent == case["intent"], route.reason
    ctx, state = state_for(env, case["as"], case["question"], route.intent)
    state.playbook = playbook_for(env.runtime.memory, state.principal, route.intent)
    call = RulePlanner().next_step(state)
    assert isinstance(call, ToolCall) and call.tool == case["first_tool"]
    assert call.tool in ALLOWED[route.intent]


def test_subjects_match_aliases_longest_first_and_first_words(env):
    ctx = env.runtime.context(principal(env, "sam"))
    names = lambda q: sorted(m.name for m in match_subjects(ctx, q))  # noqa: E731
    assert names("What depends on the SSO integration?") == ["SSO integration"]
    assert names("single sign-on status") == ["SSO"]
    assert names("Draft a note for Acme about SSO") == ["Acme Corp", "Acme renewal", "SSO"]
    assert names("Let Sam know") == ["Sam"]
    assert names("nothing here") == []


def test_a_seventh_call_is_refused(env, monkeypatch):
    monkeypatch.setattr(loop_module, "invoke", fake_invoke({}))
    ctx, state = state_for(env, "sam", "Which SSO provider?", Intent.LOOKUP)
    calls = [ToolCall("get_decision", {"topic": f"t{i}"}) for i in range(8)]
    run_loop(state, ctx, Script(calls), allowed=ALLOWED[Intent.LOOKUP], max_calls=6)
    assert state.calls == 6
    assert [s.status for s in state.trace] == ["ok"] * 6 + ["rejected"]
    assert "limit of 6" in state.trace[-1].reason


def test_a_tool_outside_the_intent_is_refused(env, monkeypatch):
    monkeypatch.setattr(loop_module, "invoke", fake_invoke({}))
    ctx, state = state_for(env, "sam", "Which SSO provider?", Intent.LOOKUP)
    calls = [ToolCall("find_owner", {"subject": "SSO"}), ToolCall("draft_email", {}), ToolCall("get_decision", {"topic": "SSO"})]
    run_loop(state, ctx, Script(calls), allowed=ALLOWED[Intent.LOOKUP], max_calls=6)
    assert [(s.tool, s.status) for s in state.trace] == [
        ("find_owner", "rejected"), ("draft_email", "rejected"), ("get_decision", "ok"),
    ]
    assert state.calls == 1


def test_ids_flow_between_steps_and_invented_ids_are_refused(env, monkeypatch):
    found = ToolResult("get_decision", {"decisions": [{"id": DECISION}]}, node_ids=[DECISION])
    monkeypatch.setattr(loop_module, "invoke", fake_invoke({"get_decision": found}))
    ctx, state = state_for(env, "sam", "Why SSO?", Intent.PROVENANCE)
    calls = [
        ToolCall("trace_decision", {"decision_id": DECISION}),
        ToolCall("get_decision", {"topic": "SSO"}),
        ToolCall("trace_decision", {"decision_id": DECISION}),
        ToolCall("trace_decision", {"decision_id": "acme:decision:made-up"}),
    ]
    run_loop(state, ctx, Script(calls), allowed=ALLOWED[Intent.PROVENANCE], max_calls=6)
    assert [s.status for s in state.trace] == ["rejected", "ok", "ok", "rejected"]
    assert "ids not seen" in state.trace[0].reason and "made-up" in state.trace[3].reason


def test_bad_arguments_are_recorded_without_calling_the_tool(env):
    ctx, state = state_for(env, "sam", "Which SSO provider?", Intent.LOOKUP)
    run_loop(state, ctx, Script([ToolCall("get_decision", {"bogus": 1})]), allowed=ALLOWED[Intent.LOOKUP], max_calls=6)
    assert state.trace[0].status == "bad_input" and "unknown arguments" in state.trace[0].reason


def _validator_state(env):
    _, state = state_for(env, "sam", "q", Intent.LOOKUP)
    state.results.append(ToolResult(
        "get_decision", {}, citations=[Citation(ITEM, "q", "Dev", None, "#eng-sso", None), RestrictedCitation(PROJ, "s")],
        path=[PathEdge("a", "OWNS", "b")],
    ))
    return state


def test_validator_drops_or_flags_claims_without_evidence_from_this_run(env):
    state = _validator_state(env)
    claims = [
        Claim("kept", [ITEM]), Claim("projection", [PROJ]), Claim("edge", ["path:a|OWNS|b"]),
        Claim("no evidence", []), Claim("invented", ["acme:slack:message:other"]), Claim("mixed", [ITEM, "x"]),
    ]
    kept, dropped = validate(claims, state, "drop")
    assert [c.text for c in kept] == ["kept", "projection", "edge"] and dropped == 3
    flagged, dropped = validate(claims, state, "flag")
    assert dropped == 0 and [c.text for c in flagged if c.flagged] == ["no evidence", "invented", "mixed"]
    assert next(c for c in flagged if c.text == "mixed").evidence_ids == [ITEM]
    with pytest.raises(ValueError):
        validate(claims, state, "ignore")


def test_template_composer_cites_decisions_and_restricted_summaries(env):
    _, state = state_for(env, "priya", "Which SSO provider?", Intent.LOOKUP)
    state.results.append(ToolResult("get_decision", {"decisions": [
        {"id": DECISION, "statement": "Use Provider B for SSO.", "status": "active", "valid_from": "2026-10-12T09:30:00Z",
         "confidence": 0.9, "likely": False, "evidence": [ITEM],
         "replaces": {"id": "acme:decision:d0", "statement": "Use Provider A for SSO.", "valid_from": "2026-10-03T10:40:00Z"}},
        {"id": "acme:decision:d3", "summary": "Leadership set SSO first.", "restricted": True, "projection_id": PROJ},
    ]}))
    claims = TemplateComposer().compose(state)
    assert claims[0].evidence_ids == [ITEM] and claims[0].confidence == 0.9
    assert claims[0].freshness == 'Active since Oct 12, 2026, replaces "Use Provider A for SSO." (Oct 3, 2026)'
    assert claims[1].restricted and claims[1].evidence_ids == [PROJ]


def test_agent_writes_one_audit_row_episode_and_turns(env):
    agent = build_agent(env.runtime)
    card = agent.answer(principal(env, "sam"), "Which SSO provider should I use?")
    rows = [r for r in env.runtime.audit.rows if r["kind"] == "answer"]
    assert len(rows) == 1 and rows[0]["intent"] == "lookup"
    assert rows[0]["tools"] == ["recall_memory", "get_decision", "search_context"]
    assert card.answer.startswith("I couldn't find") and "hidden_count" not in card.public()
    episode = env.runtime.memory.get_episode(card.episode_id)
    assert episode.question == "Which SSO provider should I use?" and episode.intent == "lookup"
    assert [t.role for t in env.runtime.memory.turns(card.conversation_id, 10)] == ["user", "agent"]


def test_channel_answers_are_stored_for_the_channel_only(env):
    agent = build_agent(env.runtime)
    channel = env.resolver.for_channel("acme", "slack:C0ENGPUB", [env.fx.ids["sam"]], Surface.SLACK_CHANNEL)
    card = agent.answer(channel, "Which SSO provider should I use?", conversation_id="slack:C0ENGPUB:1")
    episode = env.runtime.memory.get_episode(card.episode_id)
    assert episode.person_id == "slack:C0ENGPUB" and episode.audience == "slack:C0ENGPUB"
    assert "recall_memory" not in [s["tool"] for s in card.trace]
    assert recall(env.runtime.context(principal(env, "sam")), "SSO provider", 5) == []


def test_hidden_count_shows_only_with_the_notice_on(env, monkeypatch):
    monkeypatch.setattr(loop_module, "invoke", fake_invoke({"get_decision": ToolResult("get_decision", {}, hidden=2)}))
    card = build_agent(env.runtime).answer(principal(env, "sam"), "Which SSO provider should I use?")
    assert "hidden_count" not in card.public()
    on = offline_runtime(cfg=load_config().model_copy(update={"hidden_sources_notice": True}))
    card = build_agent(on.runtime).answer(principal(on, "sam"), "Which SSO provider should I use?")
    assert card.public()["hidden_count"] == 2


def test_llm_path_runs_through_the_same_guards_and_validator(env, monkeypatch):
    found = ToolResult("get_decision", {"decisions": [{"id": DECISION}]}, node_ids=[DECISION],
                       citations=[Citation(ITEM, "We are moving SSO to Provider B.", "Dev", None, "#eng-sso", None)])
    monkeypatch.setattr(loop_module, "invoke", fake_invoke({"get_decision": found}))
    llm = ScriptedLLM([
        LLMReply(data={"intent": "lookup"}),
        LLMReply(tool_calls=[LLMToolCall("find_owner", {"subject": "SSO"})]),
        LLMReply(tool_calls=[LLMToolCall("get_decision", {"decision_id": "acme:decision:invented"})]),
        LLMReply(tool_calls=[LLMToolCall("get_decision", {"topic": "SSO"})]),
        LLMReply(text="done"),
        LLMReply(data={"claims": [
            {"text": "Use Provider B.", "evidence_ids": [ITEM]},
            {"text": "Provider C is next.", "evidence_ids": ["acme:slack:message:nope"]},
        ]}),
    ])
    agent = build_agent(env.runtime, llm=llm)
    card = agent.answer(principal(env, "sam"), "Which SSO provider should I use?")
    assert [(s["tool"], s["status"]) for s in card.trace] == [
        ("recall_memory", "ok"), ("find_owner", "rejected"), ("get_decision", "rejected"), ("get_decision", "ok"),
    ]
    assert [c.text for c in card.claims] == ["Use Provider B."] and card.dropped == 1
    offered = {t["function"]["name"] for t in llm.requests[1]["tools"]}
    assert offered == ALLOWED[Intent.LOOKUP] - {"recall_memory"}


def test_llm_router_falls_back_to_rules_on_a_bad_reply():
    router, _, _ = build_components(load_config(), ScriptedLLM([LLMReply(data={"intent": "gossip"})]))
    assert router.route("Who owns the SSO integration?").intent == Intent.OWNERSHIP


def test_groq_adapter_parses_tools_and_json_and_none_stays_rule_based(monkeypatch):
    from agents.answer.llm import GroqLLM

    rules = load_config().model_copy(update={"answer": load_config().answer.model_copy(
        update={"llm": load_config().answer.llm.model_copy(update={"provider": "none"})})})
    router, planner, composer = build_components(rules)
    assert isinstance(router, RuleRouter) and isinstance(planner, RulePlanner)
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GROQ_API_KEY"):
        build_components(load_config())

    seen = {}

    def http(url, headers, body):
        seen["url"] = url
        seen["auth"] = headers["Authorization"]
        seen["agent"] = headers.get("User-Agent")
        seen["body"] = json.loads(body)
        if seen["body"].get("tools"):
            payload = {"choices": [{"message": {"content": "", "tool_calls": [{
                "function": {"name": "get_decision", "arguments": "{\"topic\": \"SSO\"}"},
            }]}}]}
        else:
            payload = {"choices": [{"message": {"content": "{\"intent\": \"lookup\"}"}}]}
        return 200, json.dumps(payload).encode()

    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    groq = GroqLLM.from_config(load_config())
    groq._http = http
    tool = groq.complete([{"role": "user", "content": "Which provider?"}], tools=[{"type": "function"}])
    parsed = groq.complete([{"role": "user", "content": "classify"}], json_schema={"type": "object"})
    assert tool.tool_calls[0].name == "get_decision" and tool.tool_calls[0].arguments == {"topic": "SSO"}
    assert parsed.data == {"intent": "lookup"}
    assert seen["auth"] == "Bearer gsk-test" and seen["agent"] == "weave-agent" and "gsk-test" not in seen["body"]
    assert "gsk-test" not in repr(groq)
    router, planner, composer = build_components(load_config())
    assert isinstance(router, LLMRouter) and isinstance(planner, RulePlanner) and isinstance(composer, LLMComposer)


def test_citation_shaped_search_hits_become_the_answer(env):
    _, state = state_for(env, "sam", "where are the Acme pilot kickoff notes", Intent.PROVENANCE)
    state.results.append(ToolResult("search_context", {"hits": [{
        "item_id": ITEM,
        "author": "Yuvraj Sanghai",
        "source": "social",
        "at": "2026-10-17T12:00:00Z",
        "snippet": "Acme pilot kickoff notes are in Drive and Notion. Next step is the weekly check-in.",
    }]}))
    scripted = ScriptedLLM([
        LLMReply(),
        LLMReply(data={"answer": "The notes are in Drive and Notion. The next step is the weekly check-in."}),
    ])
    claims = LLMComposer(scripted, env.runtime.cfg).compose(state)
    assert [claim.text for claim in claims] == [
        "The notes are in Drive and Notion. The next step is the weekly check-in.",
    ]
    assert claims[0].evidence_ids == [ITEM]
    assert " in social on " not in claims[0].text


def test_a_direct_claim_is_not_rewritten(env):
    _, state = state_for(env, "sam", "Which SSO provider?", Intent.LOOKUP)
    scripted = ScriptedLLM([
        LLMReply(data={"claims": [{"text": "Use Provider B for SSO.", "evidence_ids": [ITEM]}]}),
    ])
    claims = LLMComposer(scripted, env.runtime.cfg).compose(state)
    assert [claim.text for claim in claims] == ["Use Provider B for SSO."]
    assert len(scripted.requests) == 1


def test_groq_bucket_waits_out_a_burst_over_rpm(monkeypatch):
    from agents.answer.llm import GroqLLM

    loaded = load_config().answer.llm
    assert (loaded.provider, loaded.model, loaded.api_key_env, loaded.rpm) == (
        "groq", "openai/gpt-oss-120b", "GROQ_API_KEY", 30,
    )
    now = {"t": 0.0}
    log = []

    def clock():
        return now["t"]

    def sleep(seconds):
        log.append(("wait", seconds))
        now["t"] += seconds

    def http(url, headers, body):
        assert headers["Authorization"] == "Bearer gsk-test"
        assert b"gsk-test" not in body
        parsed = json.loads(body)
        assert "gsk-test" not in json.dumps(parsed)
        log.append(("send", clock()))
        payload = {"choices": [{"message": {"content": "{\"intent\": \"lookup\"}"}}]}
        return 200, json.dumps(payload).encode()

    tuned = load_config().model_copy(update={"answer": load_config().answer.model_copy(
        update={"llm": loaded.model_copy(update={"rpm": 2})})})
    monkeypatch.setenv("GROQ_API_KEY", "gsk-test")
    groq = GroqLLM.from_config(tuned, clock=clock, sleep=sleep)
    groq._http = http
    message = [{"role": "user", "content": "classify"}]
    for _ in range(2):
        assert groq.complete(message, json_schema={"type": "object"}).data == {"intent": "lookup"}
    assert log == [("send", 0.0), ("send", 0.0)]
    assert groq.complete(message, json_schema={"type": "object"}).data == {"intent": "lookup"}
    assert log[2][0] == "wait" and log[2][1] == pytest.approx(30)
    assert log[3][0] == "send" and log[3][1] == pytest.approx(30)


def test_scripted_and_rule_paths_do_not_take_a_groq_slot(monkeypatch):
    from agents.answer.llm import TokenBucket

    def entered(self):
        raise AssertionError("groq limiter entered")

    monkeypatch.setattr(TokenBucket, "acquire", entered)
    rules = load_config().model_copy(update={"answer": load_config().answer.model_copy(
        update={"llm": load_config().answer.llm.model_copy(update={"provider": "none"})})})
    router, planner, composer = build_components(rules)
    assert isinstance(router, RuleRouter) and isinstance(planner, RulePlanner) and isinstance(composer, TemplateComposer)
    assert router.route("Who owns the SSO integration?").intent == Intent.OWNERSHIP
    scripted = ScriptedLLM([LLMReply(data={"intent": "lookup"})])
    router, _, _ = build_components(load_config(), scripted)
    assert router.route("Which SSO provider?").intent == Intent.LOOKUP
    assert scripted.requests


def test_playbooks_fall_back_to_defaults_and_prefer_the_role(env):
    priya, sam = principal(env, "priya"), principal(env, "sam")
    assert playbook_for(env.runtime.memory, priya, Intent.DRAFT)[0] == "dependencies"
    assert playbook_for(env.runtime.memory, sam, Intent.DRAFT) == DEFAULT_PLAYBOOKS[Intent.DRAFT]
    assert playbook_for(None, sam, Intent.LOOKUP) == DEFAULT_PLAYBOOKS[Intent.LOOKUP]
    assert seed(env.runtime.memory) == []


def _client(env):
    env.store.add_token("tok-sam", env.fx.ids["sam"])
    env.agent = build_agent(env.runtime)
    app = build_app(env.runtime, agent=env.agent)
    return TestClient(app, base_url="http://127.0.0.1:8765")


def _wait_persist(env):
    thread = getattr(env.agent, "_persist_thread", None)
    if thread is not None:
        thread.join(2)


def test_answer_route_needs_a_token_and_a_question(env):
    with _client(env) as client:
        assert client.post("/answer", json={"question": "x"}).status_code == 401
        auth = {"authorization": "Bearer tok-sam"}
        assert client.post("/answer", json={}, headers=auth).status_code == 400
        assert client.post("/answer", content=b"not json", headers=auth).status_code == 400
        assert client.post("/answer", json={"question": "x" * 2001}, headers=auth).status_code == 400
        response = client.post("/answer", json={"question": "Which SSO provider should I use?"}, headers=auth)
        _wait_persist(env)
        assert response.status_code == 200 and response.json()["intent"] == "lookup"
        conv = response.json()["conversation_id"]
        follow = client.post("/answer", json={"question": "And why?", "conversation_id": conv}, headers=auth)
        _wait_persist(env)
        assert follow.status_code == 200 and follow.json()["conversation_id"] == conv
        missing = client.post("/answer", json={"question": "x", "conversation_id": "conv_nope"}, headers=auth)
        assert missing.status_code == 404
        assert client.post("/actions/task_nope/confirm", json={"decision": "accept"}, headers=auth).status_code == 404
    [row, *_] = [r for r in env.runtime.audit.rows if r["kind"] == "answer"]
    assert row["principal"].surface == "web"


def test_follow_up_reuses_the_earlier_subject(env):
    agent = build_agent(env.runtime)
    first = agent.answer(principal(env, "sam"), "Which SSO provider should I use?")
    follow = agent.answer(principal(env, "sam"), "And why?", conversation_id=first.conversation_id)
    assert follow.intent == "provenance"
    assert any(s["tool"] == "get_decision" and s["args"] == {"topic": "SSO"} for s in follow.trace)


def test_agent_class_accepts_any_components(env):
    agent = AnswerAgent(env.runtime, router=RuleRouter(), planner=Script([]), composer=TemplateComposer())
    card = agent.answer(principal(env, "dev"), "Who owns the SSO integration?")
    assert card.intent == "ownership" and [s["tool"] for s in card.trace] == ["recall_memory"]
