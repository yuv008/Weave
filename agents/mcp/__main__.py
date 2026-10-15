"""Run the agent server: MCP at /mcp, the answer API at /answer and /actions/{id}/confirm.

Usage: python -m agents.mcp [--host 127.0.0.1] [--port 8765]

Clients send ``Authorization: Bearer <token>``; tokens come from
``scripts/issue_demo_tokens.py`` (or the web app later).
"""

import argparse
import logging

import uvicorn

from agents.mcp.server import build_app
from agents.runtime import live_runtime


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    runtime = live_runtime()
    from agents.answer.agent import build_agent

    app = build_app(runtime, agent=build_agent(runtime), host=args.host)
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")


if __name__ == "__main__":
    main()
