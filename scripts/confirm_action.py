"""Accept, edit or reject your own proposal (a draft email or a note to a colleague).

Usage:
  python -m scripts.confirm_action task_... accept --as priya
  python -m scripts.confirm_action task_... edit --text "New wording" --as priya
  python -m scripts.confirm_action task_... reject --as priya [--local]

Accepting a note queues a Slack DM task for the Slack sender (Part 6 sends it).
Accepting an email keeps it a draft for you to send. Nothing is sent from here.
"""

import argparse
import json

import httpx2

from scripts.tokens import token_for


def confirm_remote(url: str, token: str, task_id: str, decision: str, text: str | None) -> dict:
    response = httpx2.post(f"{url.rstrip('/')}/actions/{task_id}/confirm", json={"decision": decision, "text": text},
                           headers={"Authorization": f"Bearer {token}"}, timeout=60)
    if response.status_code != 200:
        raise SystemExit(f"{response.status_code}: {response.text}")
    return response.json()


def confirm_local(token: str, task_id: str, decision: str, text: str | None) -> dict:
    from agents.answer.actions import ActionNotFound, confirm_action
    from agents.gate.principal import Surface
    from agents.runtime import live_runtime
    from agents.tools.base import ToolInputError

    runtime = live_runtime()
    principal = runtime.resolver.for_token(token, Surface.WEB)
    try:
        return confirm_action(runtime, principal, task_id, decision, text)
    except ActionNotFound:
        raise SystemExit("action not found") from None
    except ToolInputError as exc:
        raise SystemExit(str(exc)) from None


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("task_id")
    parser.add_argument("decision", choices=["accept", "edit", "reject"])
    parser.add_argument("--text", default=None)
    parser.add_argument("--as", dest="who", default="sam")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--local", action="store_true")
    args = parser.parse_args()

    token = token_for(args.who)
    outcome = (confirm_local(token, args.task_id, args.decision, args.text) if args.local
               else confirm_remote(args.url, token, args.task_id, args.decision, args.text))
    print(json.dumps(outcome, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
