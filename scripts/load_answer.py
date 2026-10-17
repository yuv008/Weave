"""Send questions to POST /answer from N concurrent clients and print client latency.

Usage:
  python -m scripts.load_answer
  python -m scripts.load_answer --users 50 --url http://127.0.0.1:8765 --as sam

Run the API with the rule-based agent (answer.llm.provider none) before this
script, so the run does not spend GROQ_API_KEY. Start that server yourself
(python -m agents.mcp). This process only sends HTTP and does not write to
the database. The bearer token comes from WEAVE_TOKEN_<WHO> or .env.tokens
and is never printed.
"""

import argparse
import math
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

import httpx2
import yaml

from scripts.tokens import token_for

QUESTIONS = Path(__file__).resolve().parent.parent / "tests" / "data" / "questions.yaml"
DEFAULT_URL = "http://127.0.0.1:8765"
OPERATOR_NOTICE = (
    "Run the API with the rule-based agent (answer.llm.provider none) "
    "so this load test does not spend GROQ_API_KEY. "
    "This script only sends HTTP POST /answer and does not write to the database."
)


@dataclass(frozen=True)
class Sample:
    latency_ms: float
    ok: bool
    status: int | None


def load_questions(path: Path | None = None) -> list[str]:
    """Question text from the eval set. The field in that file is ``question``."""

    path = path or QUESTIONS
    rows = yaml.safe_load(path.read_text(encoding="utf-8")) or []
    questions = []
    for row in rows:
        text = row.get("question") if isinstance(row, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise SystemExit(f"{path.name} entries need a question string")
        questions.append(text.strip())
    if not questions:
        raise SystemExit(f"no questions in {path}")
    return questions


def percentile(latencies_ms: list[float], pct: float) -> float:
    """Linear percentile of client latencies, in milliseconds. ``pct`` is 0-100."""

    if not latencies_ms:
        return 0.0
    ordered = sorted(float(value) for value in latencies_ms)
    if len(ordered) == 1:
        return ordered[0]
    rank = (pct / 100.0) * (len(ordered) - 1)
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return ordered[low]
    weight = rank - low
    return ordered[low] + (ordered[high] - ordered[low]) * weight


def summarize(latencies_ms: list[float], errors: int) -> dict[str, float | int]:
    """Count, error count, and p50 / p95 / max latency in milliseconds."""

    return {
        "count": len(latencies_ms),
        "errors": errors,
        "p50": percentile(latencies_ms, 50),
        "p95": percentile(latencies_ms, 95),
        "max": max(latencies_ms) if latencies_ms else 0.0,
    }


def format_summary(summary: dict[str, float | int]) -> str:
    return (
        f"count {summary['count']}  errors {summary['errors']}  "
        f"p50 {summary['p50']:.1f} ms  p95 {summary['p95']:.1f} ms  "
        f"max {summary['max']:.1f} ms"
    )


def measure(post, question: str) -> Sample:
    """Time one client call. ``post`` returns the HTTP status or raises."""

    started = time.perf_counter()
    status = None
    try:
        status = post(question)
        ok = status == 200
    except Exception:
        ok = False
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return Sample(elapsed_ms, ok, status)


def run_load(post, questions: list[str], users: int) -> list[Sample]:
    """One question per client, ``users`` calls in flight together."""

    if users < 1:
        raise ValueError("users must be at least 1")
    if not questions:
        raise ValueError("no questions")
    assigned = [questions[i % len(questions)] for i in range(users)]
    with ThreadPoolExecutor(max_workers=users) as pool:
        return list(pool.map(lambda question: measure(post, question), assigned))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--users", type=int, default=50, help="concurrent clients (default 50)")
    parser.add_argument("--url", default=DEFAULT_URL, help=f"API base URL (default {DEFAULT_URL})")
    parser.add_argument("--as", dest="who", default="sam", help="demo person whose bearer token is sent")
    args = parser.parse_args()
    if args.users < 1:
        raise SystemExit("--users must be at least 1")

    print(OPERATOR_NOTICE)
    token = token_for(args.who)
    questions = load_questions()
    headers = {"Authorization": f"Bearer {token}"}

    with httpx2.Client(base_url=args.url.rstrip("/"), timeout=120.0, trust_env=False) as client:
        def post(question: str) -> int:
            response = client.post("/answer", json={"question": question}, headers=headers)
            return response.status_code

        samples = run_load(post, questions, args.users)
    summary = summarize(
        [sample.latency_ms for sample in samples],
        sum(not sample.ok for sample in samples),
    )
    print(format_summary(summary))


if __name__ == "__main__":
    main()
