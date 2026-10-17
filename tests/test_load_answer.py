"""Client timing for the answer load script. In-process only: no port, no Groq, no live stores."""

import pytest
from starlette.testclient import TestClient

from agents.answer.agent import build_agent
from agents.mcp.server import build_app
from scripts.load_answer import OPERATOR_NOTICE, load_questions, run_load, summarize
from scripts.seed_playbooks import seed
from tests.answer_support import offline_runtime


def test_summary_aggregates_fake_latencies():
    latencies = [10.0, 20.0, 30.0, 40.0, 100.0]
    summary = summarize(latencies, errors=1)
    assert summary["count"] == 5
    assert summary["errors"] == 1
    assert summary["p50"] == 30.0
    assert summary["p95"] == pytest.approx(88.0)
    assert summary["max"] == 100.0
    assert "answer.llm.provider" in OPERATOR_NOTICE and "none" in OPERATOR_NOTICE


def test_three_offline_answer_requests_are_timed():
    env = offline_runtime()
    seed(env.runtime.memory)
    env.store.add_token("tok-sam", env.fx.ids["sam"])
    app = build_app(env.runtime, agent=build_agent(env.runtime))
    questions = load_questions()
    assert questions[0] == "Which SSO provider should I use?"

    with TestClient(app, base_url="http://testserver") as client:
        def post(question: str) -> int:
            response = client.post(
                "/answer",
                json={"question": question},
                headers={"Authorization": "Bearer tok-sam"},
            )
            return response.status_code

        samples = run_load(post, questions, users=3)

    summary = summarize([sample.latency_ms for sample in samples], sum(not sample.ok for sample in samples))
    assert [sample.status for sample in samples] == [200, 200, 200]
    assert summary["count"] == 3 and summary["errors"] == 0
    assert summary["max"] >= summary["p95"] >= summary["p50"] >= 0
