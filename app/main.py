"""HTTP entrypoint for Slack events.

The demo tenant, connection, and workspace come from the environment.
There is no per-user ACL on this route.
"""

import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.slack.events import apply_slack_event, verify_slack_signature


def create_app(
    *,
    graph=None,
    signing_secret: str | None = None,
    tenant_id: str | None = None,
    connection_id: str | None = None,
    workspace_id: str | None = None,
    workspace_domain: str | None = None,
    now=None,
) -> FastAPI:
    app = FastAPI()
    app.state.graph = graph
    app.state.signing_secret = signing_secret if signing_secret is not None else os.environ.get("SLACK_SIGNING_SECRET", "")
    app.state.tenant_id = tenant_id if tenant_id is not None else os.environ.get("WEAVE_TENANT_ID", "")
    app.state.connection_id = connection_id if connection_id is not None else os.environ.get("WEAVE_CONNECTION_ID", "")
    app.state.workspace_id = workspace_id if workspace_id is not None else os.environ.get("WEAVE_WORKSPACE_ID", "")
    app.state.workspace_domain = (
        workspace_domain if workspace_domain is not None else os.environ.get("SLACK_WORKSPACE_DOMAIN", "")
    )
    app.state.now = now or time.time

    @app.post("/slack/events")
    async def slack_events(request: Request):
        body = await request.body()
        if not verify_slack_signature(
            app.state.signing_secret,
            request.headers.get("x-slack-request-timestamp", ""),
            body,
            request.headers.get("x-slack-signature", ""),
            now=app.state.now(),
        ):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        payload = json.loads(body)
        target = app.state.graph
        if target is None:
            from app.graph.client import connect_graph

            target = connect_graph()
        result = apply_slack_event(
            payload,
            target,
            tenant_id=app.state.tenant_id,
            connection_id=app.state.connection_id,
            workspace_id=app.state.workspace_id,
            workspace_domain=app.state.workspace_domain or None,
        )
        return result

    return app


app = create_app()
