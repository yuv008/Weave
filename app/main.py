"""HTTP entrypoint for Slack events.

The demo tenant, connection, and workspace come from the environment.
There is no per-user ACL on this route.
"""

import json
import os
import time

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from app.answer.claims import record_commitment, record_figure, supersede_claim
from app.answer.decision import channel_decisions
from app.ingest.records import MemoryStore, postgres_store
from app.github.events import apply_github_event, verify_github_signature
from app.slack.events import apply_slack_event, verify_slack_signature


def _records(app):
    if app.state.records is None:
        app.state.records = postgres_store()
    return app.state.records


def create_app(
    *,
    graph=None,
    signing_secret: str | None = None,
    tenant_id: str | None = None,
    connection_id: str | None = None,
    workspace_id: str | None = None,
    workspace_domain: str | None = None,
    now=None,
    github_secret: str | None = None,
    github_owner: str | None = None,
    github_repo: str | None = None,
    github_connection_id: str | None = None,
    store=None,
) -> FastAPI:
    app = FastAPI()
    app.state.graph = graph
    if store is not None:
        app.state.records = store
    elif graph is not None:
        app.state.records = MemoryStore()
    else:
        app.state.records = None
    app.state.signing_secret = signing_secret if signing_secret is not None else os.environ.get("SLACK_SIGNING_SECRET", "")
    app.state.tenant_id = tenant_id if tenant_id is not None else os.environ.get("WEAVE_TENANT_ID", "")
    app.state.connection_id = connection_id if connection_id is not None else os.environ.get("WEAVE_CONNECTION_ID", "")
    app.state.workspace_id = workspace_id if workspace_id is not None else os.environ.get("WEAVE_WORKSPACE_ID", "")
    app.state.workspace_domain = (
        workspace_domain if workspace_domain is not None else os.environ.get("SLACK_WORKSPACE_DOMAIN", "")
    )
    app.state.now = now or time.time
    app.state.github_secret = github_secret if github_secret is not None else os.environ.get("GITHUB_WEBHOOK_SECRET", "")
    app.state.github_owner = github_owner if github_owner is not None else os.environ.get("GITHUB_OWNER", "")
    app.state.github_repo = github_repo if github_repo is not None else os.environ.get("GITHUB_REPO", "")
    app.state.github_connection_id = (
        github_connection_id if github_connection_id is not None else os.environ.get("GITHUB_CONNECTION_ID", "")
    )

    @app.get("/health")
    def health():
        return {"ok": True}

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
            store=_records(app),
            tenant_id=app.state.tenant_id,
            connection_id=app.state.connection_id,
            workspace_id=app.state.workspace_id,
            workspace_domain=app.state.workspace_domain or None,
        )
        return result

    @app.post("/github/webhook")
    async def github_webhook(request: Request):
        body = await request.body()
        if not verify_github_signature(
            app.state.github_secret,
            body,
            request.headers.get("x-hub-signature-256", ""),
        ):
            return JSONResponse({"error": "invalid signature"}, status_code=401)
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid json"}, status_code=400)
        target = app.state.graph
        if target is None:
            from app.graph.client import connect_graph

            target = connect_graph()
        return apply_github_event(
            payload,
            target,
            event=request.headers.get("x-github-event", ""),
            tenant_id=app.state.tenant_id,
            connection_id=app.state.github_connection_id,
            owner=app.state.github_owner,
            repo=app.state.github_repo,
            delivery_id=request.headers.get("x-github-delivery", ""),
        )

    @app.post("/slack/decisions")
    async def slack_decisions(request: Request):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid json"}, status_code=400)
        target = app.state.graph
        if target is None:
            from app.graph.client import connect_graph

            target = connect_graph()
        try:
            decisions = channel_decisions(
                target,
                app.state.tenant_id,
                payload.get("channel_id") or "",
                payload.get("statement") or "",
                payload.get("quote") or "",
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"decisions": [decision.as_dict() for decision in decisions]}

    async def _claim(request: Request, handler):
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid json"}, status_code=400)
        target = app.state.graph
        if target is None:
            from app.graph.client import connect_graph

            target = connect_graph()
        try:
            return handler(target, _records(app), app.state.tenant_id, payload)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)

    @app.post("/claims/commitment")
    async def claim_commitment(request: Request):
        return await _claim(request, lambda graph, store, tenant, body: {"id": record_commitment(graph, store, tenant, body)})

    @app.post("/claims/figure")
    async def claim_figure(request: Request):
        return await _claim(request, lambda graph, store, tenant, body: {"id": record_figure(graph, store, tenant, body)})

    @app.post("/claims/supersede")
    async def claim_supersede(request: Request):
        return await _claim(request, supersede_claim)

    return app


app = create_app()
