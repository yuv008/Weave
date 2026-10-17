"""Setup page and OAuth routes on the webhook app."""

import json
import logging
import os
import secrets
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app.connect.oauth import CARDS, MemoryStateStore, StateMismatch, begin, complete, configured

log = logging.getLogger("weave.connect")
PAGE = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
_CONNECTABLE = {"slack", "notion", "google", "github"}


def register_connect(app: FastAPI) -> None:
    app.state.oauth_states = MemoryStateStore()
    if getattr(app.state, "poll_now", None) is None:
        app.state.poll_now = _noop_poll if app.state.graph is not None or app.state.records is not None else run_poll

    @app.get("/")
    def setup_page():
        if PAGE.is_file():
            return FileResponse(PAGE)
        return JSONResponse({"error": "setup page is missing"}, status_code=404)

    @app.get("/setup/status")
    def setup_status():
        return _status(app)

    @app.post("/setup/company")
    async def setup_company(request: Request):
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 80:
            return JSONResponse({"error": "enter a company name"}, status_code=400)
        try:
            _put(app, "profile", name, {"name": name}, status="connected")
        except Exception as exc:
            log.warning("company was not saved (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not save the company"}, status_code=503)
        return {"name": name}

    @app.post("/setup/model")
    async def setup_model(request: Request):
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        key = str(body.get("key") or "").strip()
        if not key:
            return {"model": "default"}
        if len(key) > 400:
            return JSONResponse({"error": "that key is too long"}, status_code=400)
        try:
            _put(app, "groq", "groq", {"token": key}, status="connected")
        except Exception as exc:
            log.warning("model key was not saved (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not save the model key"}, status_code=503)
        return {"model": "saved"}

    @app.post("/setup/google/folder")
    async def setup_folder(request: Request, background: BackgroundTasks):
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        folder = str(body.get("folder_id") or "").strip()
        if not folder or len(folder) > 200:
            return JSONResponse({"error": "enter the Drive folder id"}, status_code=400)
        if not _merge(app, "google", folder_id=folder):
            return JSONResponse({"error": "connect Google first"}, status_code=400)
        _schedule(app, background, "google")
        return {"folder_id": folder}

    @app.post("/setup/github/repo")
    async def setup_repo(request: Request, background: BackgroundTasks):
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        owner = str(body.get("owner") or "").strip()
        repo = str(body.get("repo") or "").strip()
        if not owner or not repo or "/" in owner or "/" in repo:
            return JSONResponse({"error": "enter the repository owner and name"}, status_code=400)
        if not _merge(app, "github", owner=owner, repo=repo):
            return JSONResponse({"error": "connect GitHub first"}, status_code=400)
        _schedule(app, background, "github")
        return {"owner": owner, "repo": repo}

    @app.post("/setup/open")
    async def setup_open():
        tenant = _tenant(app)
        status = _status(app)
        ready = [card for card in status["connectors"] if card["id"] in _CONNECTABLE and card["status"] in {"connected", "syncing"}]
        if not ready:
            return JSONResponse({"error": "connect a tool first"}, status_code=400)
        email = os.environ.get("WEAVE_VIEWER_EMAIL", "").strip()
        if not email:
            return JSONResponse({"error": "the server has no viewer email"}, status_code=400)
        try:
            token = _mint(tenant, email, app)
        except Exception as exc:
            log.warning("viewer token was not minted (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not create a viewer token"}, status_code=500)
        agent = os.environ.get("WEAVE_AGENT_URL", "http://127.0.0.1:8765").rstrip("/")
        return {
            "token": token,
            "mcp": {
                "mcpServers": {
                    "weave": {
                        "url": f"{agent}/mcp",
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                }
            },
            "pet": {"cue_url": f"{agent}/wisp/cue", "token": token},
        }

    @app.get("/connect/{provider}")
    def connect_start(provider: str, request: Request):
        if provider not in _CONNECTABLE:
            return JSONResponse({"error": "not available"}, status_code=404)
        ready, reason = configured(provider)
        if not ready:
            return JSONResponse({"error": reason}, status_code=404)
        try:
            url = begin(provider, _tenant(app), _redirect(request, provider), app.state.oauth_states)
        except Exception as exc:
            log.warning("connect did not start (%s)", type(exc).__name__)
            return JSONResponse({"error": "connect did not start"}, status_code=400)
        return RedirectResponse(url, status_code=302)

    @app.get("/connect/{provider}/callback")
    def connect_callback(provider: str, request: Request, background: BackgroundTasks):
        if provider not in _CONNECTABLE:
            return JSONResponse({"error": "not available"}, status_code=404)
        query = dict(request.query_params)
        try:
            complete(
                provider,
                query.get("state") or "",
                query,
                app.state.oauth_states,
                _store(app),
                redirect_uri=_redirect(request, provider),
            )
        except StateMismatch:
            return RedirectResponse("/?error=state", status_code=302)
        except Exception as exc:
            log.warning("connect callback failed (%s)", type(exc).__name__)
            return RedirectResponse("/?error=connect", status_code=302)
        _schedule(app, background, provider)
        extra = "&pick=folder" if provider == "google" else ""
        return RedirectResponse(f"/?connected={provider}{extra}", status_code=302)


def run_poll(provider: str, tenant: str) -> None:
    """One immediate sync after a connect. Connector errors propagate to the caller."""

    if provider == "slack":
        from scripts.run_worker import _slack_once

        _slack_once()
    elif provider == "notion":
        from scripts.run_worker import _notion_once

        _notion_once()
    elif provider == "google":
        from scripts.run_worker import _drive_once

        _drive_once()
    elif provider == "github":
        from scripts.run_worker import _github_once

        _github_once()


def _noop_poll(provider: str, tenant: str) -> None:
    return None


def _schedule(app, background: BackgroundTasks, provider: str) -> None:
    tenant = _tenant(app)

    def job() -> None:
        store = _store(app)
        try:
            app.state.poll_now(provider, tenant)
            store.update_status(tenant, provider, "connected")
        except Exception as exc:
            log.warning("connect poll failed (%s)", type(exc).__name__)
            try:
                store.update_status(tenant, provider, "failed")
            except Exception:
                pass

    background.add_task(job)


def _status(app) -> dict:
    tenant = _tenant(app)
    store = _store(app)
    profile = _safe_get(store, tenant, "profile")
    groq = _safe_get(store, tenant, "groq")
    cards = []
    for provider, name in CARDS:
        ready, reason = configured(provider)
        found = _safe_get(store, tenant, provider) if provider in _CONNECTABLE else None
        status = "unavailable"
        needs: list[str] = []
        if found and provider in _CONNECTABLE:
            status = found.get("status") or "connected"
            reason = ""
        elif provider in _CONNECTABLE and ready:
            status = "disconnected"
            reason = ""
        if provider == "google" and found and not found.get("folder_id"):
            needs.append("folder")
        if provider == "github" and found and not (found.get("owner") and found.get("repo")):
            needs.append("repo")
        cards.append({
            "id": provider,
            "name": name,
            "status": status,
            "reason": reason if status == "unavailable" else "",
            "connectable": provider in _CONNECTABLE and ready and status == "disconnected",
            "needs": needs,
        })
    return {
        "company": str((profile or {}).get("name") or ""),
        "model": "saved" if groq and groq.get("token") else "default",
        "connectors": cards,
    }


def _tenant(app) -> str:
    return (app.state.tenant_id or os.environ.get("WEAVE_TENANT_ID") or os.environ.get("WEAVE_TENANT") or "real").strip()


def _store(app):
    store = app.state.secrets
    if store is None:
        from app.connect.credentials import current_store

        store = current_store()
        app.state.secrets = store
    return store


def _safe_get(store, tenant: str, provider: str):
    try:
        return store.get(tenant, provider)
    except Exception:
        return None


def _put(app, provider: str, external_id: str, payload: dict, *, status: str) -> None:
    _store(app).put(_tenant(app), provider, external_id, payload, status=status)


def _merge(app, provider: str, **fields) -> bool:
    store = _store(app)
    tenant = _tenant(app)
    current = _safe_get(store, tenant, provider)
    if not current or not (current.get("token") or current.get("installation_id") or current.get("refresh_token")):
        return False
    external = str(current.get("external_id") or "")
    scopes = str(current.get("scopes") or "")
    body = {key: value for key, value in current.items() if key not in {"external_id", "scopes", "status"}}
    body.update(fields)
    store.put(tenant, provider, external, body, scopes=scopes, status="syncing")
    return True


def _redirect(request: Request, provider: str) -> str:
    base = os.environ.get("WEAVE_PUBLIC_URL", str(request.base_url)).rstrip("/")
    return f"{base}/connect/{provider}/callback"


async def _body(request: Request):
    try:
        payload = await request.json()
    except json.JSONDecodeError:
        return JSONResponse({"error": "invalid json"}, status_code=400)
    if not isinstance(payload, dict):
        return JSONResponse({"error": "invalid json"}, status_code=400)
    return payload


def _mint(tenant: str, email: str, app) -> str:
    from agents.gate.resolve import hash_token
    from shared.ids import person_id

    token = f"wv_{secrets.token_urlsafe(24)}"
    connect = app.state.pg_connect if getattr(app.state, "pg_connect", None) else None
    if connect is None:
        from shared.pg import connect_pg

        connect = connect_pg
    conn = connect()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_token (token_sha256, tenant_id, person_id, label) VALUES (%s, %s, %s, %s)",
                (hash_token(token), tenant, person_id(tenant, email), "setup"),
            )
        conn.commit()
    finally:
        conn.close()
    return token
