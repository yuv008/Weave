"""Run the agent server: MCP at /mcp, the answer API at /answer and /actions/{id}/confirm, POST /wisp/cue, and GET /ops.

Usage: python -m agents.mcp [--host 127.0.0.1] [--port 8765]

Clients send ``Authorization: Bearer <token>``; tokens come from
``scripts/issue_demo_tokens.py`` (or the web app later). ``GET /ops`` is
separate: it requires ``OPS_TOKEN`` and reads ``WEAVE_TENANT``.
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
    uvicorn.run(
        app,
        host=args.host,
        port=args.port,
        log_level="info",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


if __name__ == "__main__":
    main()
