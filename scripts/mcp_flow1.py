"""Flow 1 (the day-3 check): Sam asks the running MCP server for the SSO decision.

Usage: python -m scripts.mcp_flow1 [--url http://127.0.0.1:8765/mcp] [--who SAM] [--topic SSO]

Reads ``WEAVE_TOKEN_<WHO>`` from the environment or ``.env.tokens`` and never
prints it. Prints the decision and its citations as Cursor would receive them.
"""

import argparse
import os
from pathlib import Path

import anyio
import httpx2
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

TOKENS = Path(__file__).resolve().parent.parent / ".env.tokens"


def token_for(who: str) -> str:
    key = f"WEAVE_TOKEN_{who.upper()}"
    if os.environ.get(key):
        return os.environ[key]
    if TOKENS.exists():
        for line in TOKENS.read_text(encoding="utf-8").splitlines():
            name, _, value = line.partition("=")
            if name.strip() == key and value.strip():
                return value.strip()
    raise SystemExit(f"{key} not set; run: python -m scripts.issue_demo_tokens > .env.tokens")


async def run(url: str, token: str, topic: str) -> None:
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}, timeout=30) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            tools = await client.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))
            result = await client.call_tool("get_decision", {"topic": topic})
    if result.is_error:
        raise SystemExit(f"tool error: {result.content[0].text}")
    body = result.structured_content
    if body.get("message"):
        print("message:", body["message"])
    for card in body["data"]["decisions"]:
        if card.get("restricted"):
            print(f"decision (restricted summary): {card['summary']}")
            continue
        print(f"decision: {card['statement']}  [{card['id']}]")
        if card.get("replaces"):
            print(f"  replaces: {card['replaces']['statement']}")
    for cite in body["citations"]:
        if cite["source"] == "restricted":
            print(f"  cite (restricted): {cite['summary']}")
        else:
            print(f"  cite: {cite['author']} in {cite['source']} at {cite['at']}: \"{cite['quote']}\"\n        {cite['url']}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="http://127.0.0.1:8765/mcp")
    parser.add_argument("--who", default="SAM")
    parser.add_argument("--topic", default="SSO")
    args = parser.parse_args()
    anyio.run(run, args.url, token_for(args.who), args.topic)


if __name__ == "__main__":
    main()
