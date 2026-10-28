"""HTTP entrypoint for Slack events.

The demo tenant, connection, and workspace come from the environment.
There is no per-user ACL on this route.
"""

import json
import logging
import os
import time

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import JSONResponse

from app.answer.claims import record_commitment, record_figure, supersede_claim
from app.answer.decision import channel_decisions
from app.ingest.records import MemoryStore, postgres_store
from app.github.events import apply_github_event, verify_github_signature
from app.slack.events import apply_slack_event, verify_slack_signature

_AUTO = object()
log = logging.getLogger("weave.webhook")


def _records(app):
    if app.state.records is None:
        app.state.records = postgres_store()
    return app.state.records


def _bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return ""
    return header.split(" ", 1)[1].strip()


def _viewer_tenant(token: str) -> str | None:
    """The company of a live ``api_token`` row. Failures stay a refusal."""

    try:
        from agents.gate.resolve import hash_token
        from shared.pg import connect_pg

        conn = connect_pg()
        try:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT tenant_id FROM api_token WHERE token_sha256 = %s AND revoked_at IS NULL",
                    (hash_token(token),),
                )
                row = cur.fetchone()
                return str(row[0]) if row and row[0] else None
        finally:
            conn.close()
    except Exception:
        return None


def _viewer(app, request: Request) -> str | JSONResponse:
    """The token's company, or a 401. The env tenant is never used here."""

    token = _bearer(request)
    checker = app.state.viewer_auth
    tenant = (checker(token) if checker is not None else _viewer_tenant(token)) if token else None
    if not isinstance(tenant, str) or not tenant:
        return JSONResponse({"error": "not authorized"}, status_code=401)
    return tenant


def _company_graph(app, tenant: str):
    if app.state.graph is not None:
        return app.state.graph
    from app.graph.client import connect_graph

    return connect_graph(tenant=tenant)


def _slack_event_tenant(app, payload: dict) -> str | None:
    """The company that owns this Slack workspace, or ``None`` when no company does.

    A stored grant names the company. The env tenant owns only the env workspace.
    A failed lookup raises, so Slack retries instead of the event landing in the wrong company.
    """

    team = str(payload.get("team_id") or "")
    if not team:
        return None
    from app.connect.credentials import current_store

    found = current_store().find_tenant("slack", team)
    if found and found != "acme":
        return found
    if team == app.state.workspace_id and app.state.tenant_id:
        return app.state.tenant_id
    return None


def _claims_for(app, payload: dict, tenant: str | None = None):
    """Claim store for a mention or DM. ``None`` when this process does not answer Slack."""

    from agents.slack.accept import is_answer_event

    event = payload.get("event") if isinstance(payload.get("event"), dict) else {}
    if not is_answer_event(event):
        return None
    if app.state.reply_claims is not _AUTO:
        return app.state.reply_claims
    from app.connect.credentials import slack_bot_token

    if not slack_bot_token(tenant or app.state.tenant_id):
        return None
    from agents.slack.claims import PostgresReplyClaims
    from shared.pg import connect_pg

    return PostgresReplyClaims(connect_pg)


def _run_slack_reply(app, payload: dict, claims, tenant: str | None = None) -> None:
    runner = app.state.slack_runner
    if runner is not None:
        runner(payload)
        return
    from app.connect.credentials import slack_bot_token

    chosen = tenant or app.state.tenant_id
    token = slack_bot_token(chosen)
    if not token or claims is None:
        return
    from agents.answer.agent import build_agent
    from agents.runtime import live_runtime
    from agents.slack.client import SlackApi
    from agents.slack.people import parse_user_map
    from agents.slack.reply import answer_event

    runtime = live_runtime()
    answer_event(
        payload, runtime=runtime, agent=build_agent(runtime), slack=SlackApi(token), claims=claims,
        tenant=chosen or runtime.cfg.tenant,
        user_map=parse_user_map(os.environ.get("SLACK_USER_MAP")),
    )


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
    reply_claims=_AUTO,
    slack_runner=None,
    viewer_auth=None,
    secrets=None,
    accounts=None,
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
    app.state.reply_claims = reply_claims
    app.state.slack_runner = slack_runner
    app.state.viewer_auth = viewer_auth
    if secrets is not None:
        app.state.secrets = secrets
    elif graph is not None or store is not None:
        from app.connect.secrets import MemorySecretStore

        app.state.secrets = MemorySecretStore()
    else:
        app.state.secrets = None
    if accounts is not None:
        app.state.accounts = accounts
    elif store is None and graph is None and os.environ.get("DATABASE_URL"):
        from app.connect.accounts import PostgresAccountStore

        app.state.accounts = PostgresAccountStore()
    else:
        from app.connect.accounts import MemoryAccountStore

        app.state.accounts = MemoryAccountStore()
    from app.connect.credentials import use_store
    from app.connect.routes import register_connect

    use_store(app.state.secrets)
    register_connect(app)
    if graph is None and store is None and os.environ.get("DATABASE_URL"):
        try:
            from shared.pg import warm_pg

            warm_pg()
        except Exception:
            pass

    @app.get("/health")
    def health():
        return {"ok": True}

    @app.post("/slack/events")
    async def slack_events(request: Request, background: BackgroundTasks):
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
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid json"}, status_code=400)

        tenant = None
        if payload.get("type") == "event_callback":
            try:
                tenant = _slack_event_tenant(app, payload)
            except Exception as exc:
                log.warning("slack workspace lookup failed (%s)", type(exc).__name__)
                return JSONResponse({"error": "unavailable"}, status_code=503)
            if tenant is None:
                # 200 so Slack does not retry an event no company owns.
                log.warning("slack event dropped: no company owns team %s", str(payload.get("team_id") or "-")[:32])
                return {"ok": True}
        workspace = str(payload.get("team_id") or app.state.workspace_id or "")

        def ingest() -> None:
            target = app.state.graph
            if target is None:
                from app.graph.client import connect_graph

                target = connect_graph(tenant=tenant or None)
            apply_slack_event(
                payload,
                target,
                store=_records(app),
                tenant_id=tenant,
                connection_id=app.state.connection_id,
                workspace_id=workspace,
                workspace_domain=app.state.workspace_domain or None,
            )

        claims = _claims_for(app, payload, tenant)

        def reply() -> None:
            _run_slack_reply(app, payload, claims, tenant)

        from agents.slack.accept import accept

        return accept(
            payload, ingest=ingest, reply=reply, spawn=background.add_task, claims=claims,
            tenant=tenant,
        )

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
        tenant = _viewer(app, request)
        if isinstance(tenant, JSONResponse):
            return tenant
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid json"}, status_code=400)
        try:
            decisions = channel_decisions(
                _company_graph(app, tenant),
                tenant,
                payload.get("channel_id") or "",
                payload.get("statement") or "",
                payload.get("quote") or "",
            )
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"decisions": [decision.as_dict() for decision in decisions]}

    async def _claim(request: Request, handler):
        tenant = _viewer(app, request)
        if isinstance(tenant, JSONResponse):
            return tenant
        try:
            payload = await request.json()
        except json.JSONDecodeError:
            return JSONResponse({"error": "invalid json"}, status_code=400)
        if not isinstance(payload, dict):
            return JSONResponse({"error": "invalid json"}, status_code=400)
        try:
            return handler(_company_graph(app, tenant), _records(app), tenant, payload)
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
