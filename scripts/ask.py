"""Ask the answer agent a question as a demo person and print the answer card.

Usage:
  python -m scripts.ask --as sam "Which SSO provider should I use?"
  python -m scripts.ask --as priya --conversation conv_... "And who owns it?"
  python -m scripts.ask --as sam --local "..."   (in-process, no server needed)
  python -m scripts.ask --as sam --json "..."

By default it calls ``POST /answer`` on the running server (``python -m agents.mcp``).
The token comes from ``WEAVE_TOKEN_<WHO>`` or ``.env.tokens`` and is never printed.
"""

import argparse
import json

import httpx2

from scripts.tokens import token_for


def render(card: dict) -> str:
    lines = [f"[{card['intent']}] {card['answer']}", ""]
    for i, claim in enumerate(card.get("claims", []), 1):
        tags = [t for t in ("likely", "restricted", "unverified", "stale") if claim.get(t)]
        lines.append(f"{i}. {claim['text']}" + (f"  ({', '.join(tags)})" if tags else ""))
        for ev in claim["evidence"]:
            entry = card["evidence"].get(ev, {})
            if entry.get("kind") == "projection":
                lines.append(f"     - restricted summary: {entry.get('summary')}")
            elif entry.get("kind") == "path":
                lines.append(f"     - graph: {entry['src']} -{entry['rel']}-> {entry['dst']}")
            elif entry:
                lines.append(f"     - {entry.get('author')} in {entry.get('source')} at {entry.get('at')}"
                             f": \"{entry.get('quote')}\"  {entry.get('url') or ''}".rstrip())
    if card.get("freshness"):
        lines += ["", "Freshness: " + "; ".join(card["freshness"])]
    if card.get("confidence") is not None:
        lines.append(f"Confidence: {card['confidence']}" + (" (likely)" if card.get("likely") else ""))
    if card.get("conflicts"):
        lines.append("Conflict: " + " | ".join(card["conflicts"]))
    if card.get("path"):
        lines.append(f"Path: {len(card['path'])} edges")
    proposal = card.get("proposal")
    if proposal:
        lines += ["", f"Proposal {proposal['proposal_id']} ({proposal['tool']}, {proposal['state']}, not sent):"]
        lines.append(proposal.get("draft") or f"To {proposal.get('recipient_name')}: {proposal.get('message')}")
        lines.append(f"Confirm: python -m scripts.confirm_action {proposal['proposal_id']} accept")
    lines += ["", "Tools: " + ", ".join(f"{s['tool']}({s['status']})" for s in card.get("trace", []))]
    if card.get("conversation_id"):
        lines.append(f"Conversation: {card['conversation_id']}")
    return "\n".join(lines)


def ask_remote(url: str, token: str, question: str, conversation_id: str | None) -> dict:
    body = {"question": question, **({"conversation_id": conversation_id} if conversation_id else {})}
    response = httpx2.post(f"{url.rstrip('/')}/answer", json=body,
                           headers={"Authorization": f"Bearer {token}"}, timeout=120)
    if response.status_code != 200:
        raise SystemExit(f"{response.status_code}: {response.text}")
    return response.json()


def ask_local(token: str, question: str, conversation_id: str | None) -> dict:
    from agents.answer.agent import build_agent
    from agents.gate.principal import Surface
    from agents.runtime import live_runtime

    runtime = live_runtime()
    principal = runtime.resolver.for_token(token, Surface.WEB)
    return build_agent(runtime).answer(principal, question, conversation_id=conversation_id).public()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("question")
    parser.add_argument("--as", dest="who", default="sam")
    parser.add_argument("--url", default="http://127.0.0.1:8765")
    parser.add_argument("--conversation", default=None)
    parser.add_argument("--local", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    token = token_for(args.who)
    card = (ask_local(token, args.question, args.conversation) if args.local
            else ask_remote(args.url, token, args.question, args.conversation))
    print(json.dumps(card, indent=2, ensure_ascii=False) if args.json else render(card))


if __name__ == "__main__":
    main()
