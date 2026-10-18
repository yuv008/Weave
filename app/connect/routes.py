"""Setup page and OAuth routes on the webhook app."""

import json
import logging
import os
import secrets
import urllib.parse
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app.connect.oauth import CARDS, MemoryStateStore, StateMismatch, begin, complete, configured

log = logging.getLogger("weave.connect")
PAGE = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
LANDING = PAGE.with_name("landing.html")
LOGIN = PAGE.with_name("login.html")
SIGNUP = PAGE.with_name("signup.html")
BRAND = {"mark.png", "icon.png", "wisp.svg"}
SESSION_COOKIE = "weave_session"
SESSION_MAX_AGE = 14 * 24 * 3600
LANDING_FILES = {"stage.js": "text/javascript", "stage.css": "text/css"}
_CONNECTABLE = {"slack", "notion", "google", "github"}


def register_connect(app: FastAPI) -> None:
    app.state.oauth_states = MemoryStateStore()
    if getattr(app.state, "poll_now", None) is None:
        app.state.poll_now = _noop_poll if app.state.graph is not None or app.state.records is not None else run_poll

    @app.get("/")
    def landing_page():
        if LANDING.is_file():
            return FileResponse(LANDING)
        return JSONResponse({"error": "landing page is missing"}, status_code=404)

    @app.get("/enter")
    def enter(request: Request):
        if _session(app, request):
            return RedirectResponse("/setup", status_code=303)
        return RedirectResponse("/signup", status_code=303)

    @app.get("/login")
    def login_page(request: Request):
        if _session(app, request):
            return RedirectResponse("/setup", status_code=303)
        if not LOGIN.is_file():
            return JSONResponse({"error": "login page is missing"}, status_code=404)
        return FileResponse(LOGIN)

    @app.post("/login")
    async def login_submit(request: Request):
        fields = _form(await request.body())
        result = app.state.accounts.login(fields.get("email", ""), fields.get("password", ""))
        if result is None:
            return RedirectResponse("/login?error=1", status_code=303)
        raw, _found = result
        response = RedirectResponse("/setup", status_code=303)
        _cookie(response, request, raw)
        return response

    @app.get("/signup")
    def signup_page(request: Request):
        if _session(app, request):
            return RedirectResponse("/setup", status_code=303)
        if not SIGNUP.is_file():
            return JSONResponse({"error": "signup page is missing"}, status_code=404)
        return FileResponse(SIGNUP)

    @app.post("/signup")
    async def signup_submit(request: Request):
        from app.connect.accounts import AccountError, remember_owner

        fields = _form(await request.body())
        try:
            raw, found = app.state.accounts.signup(
                email=fields.get("email", ""),
                password=fields.get("password", ""),
                name=fields.get("name", ""),
                company=fields.get("company", ""),
            )
        except AccountError as exc:
            return RedirectResponse(f"/signup?error={exc.code}", status_code=303)
        try:
            _store(app).put(
                found["tenant_id"], "profile", found["tenant_id"],
                {"name": fields.get("company", "").strip()}, status="connected",
            )
        except Exception as exc:
            log.warning("company profile was not saved (%s)", type(exc).__name__)
        remember_owner(found["tenant_id"], found["email"], found["name"])
        response = RedirectResponse("/setup", status_code=303)
        _cookie(response, request, raw)
        return response

    @app.get("/session")
    def session_page(request: Request):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        return {
            "email": found["email"],
            "name": found["name"],
            "tenant": found["tenant_id"],
            "memberships": found["memberships"],
        }

    @app.post("/session/tenant")
    async def session_tenant(request: Request):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        raw = request.cookies.get(SESSION_COOKIE, "")
        switched = app.state.accounts.switch(raw, str(body.get("tenant") or ""))
        if switched is None:
            return JSONResponse({"error": "that company is not yours"}, status_code=403)
        return {"tenant": switched["tenant_id"]}

    @app.get("/setup")
    def setup_page(request: Request):
        if _session(app, request) is None:
            return RedirectResponse("/login", status_code=303)
        if PAGE.is_file():
            return FileResponse(PAGE)
        return JSONResponse({"error": "setup page is missing"}, status_code=404)

    @app.get("/landing/{name}")
    def landing_asset(name: str):
        media = LANDING_FILES.get(name)
        if media is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        path = PAGE.parent / "landing" / name
        if not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path, media_type=media)

    @app.get("/brand/{name}")
    def brand_asset(name: str):
        if name not in BRAND:
            return JSONResponse({"error": "not found"}, status_code=404)
        path = PAGE.parent / name
        if not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path)

    @app.get("/setup/status")
    def setup_status(request: Request):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        return _status(app, found["tenant_id"])

    @app.post("/setup/company")
    async def setup_company(request: Request):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        name = str(body.get("name") or "").strip()
        if not name or len(name) > 80:
            return JSONResponse({"error": "enter a company name"}, status_code=400)
        try:
            _put(app, "profile", name, {"name": name}, status="connected", tenant=found["tenant_id"])
        except Exception as exc:
            log.warning("company was not saved (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not save the company"}, status_code=503)
        return {"name": name}

    @app.post("/setup/model")
    async def setup_model(request: Request):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        key = str(body.get("key") or "").strip()
        if not key:
            return {"model": "default"}
        if len(key) > 400:
            return JSONResponse({"error": "that key is too long"}, status_code=400)
        try:
            _put(app, "groq", "groq", {"token": key}, status="connected", tenant=found["tenant_id"])
        except Exception as exc:
            log.warning("model key was not saved (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not save the model key"}, status_code=503)
        return {"model": "saved"}

    @app.post("/setup/google/folder")
    async def setup_folder(request: Request, background: BackgroundTasks):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        folder = str(body.get("folder_id") or "").strip()
        if not folder or len(folder) > 200:
            return JSONResponse({"error": "enter the Drive folder id"}, status_code=400)
        if not _merge(app, "google", found["tenant_id"], folder_id=folder):
            return JSONResponse({"error": "connect Google first"}, status_code=400)
        _schedule(app, background, "google", found["tenant_id"])
        return {"folder_id": folder}

    @app.post("/setup/github/repo")
    async def setup_repo(request: Request, background: BackgroundTasks):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        owner = str(body.get("owner") or "").strip()
        repo = str(body.get("repo") or "").strip()
        if not owner or not repo or "/" in owner or "/" in repo:
            return JSONResponse({"error": "enter the repository owner and name"}, status_code=400)
        if not _merge(app, "github", found["tenant_id"], owner=owner, repo=repo):
            return JSONResponse({"error": "connect GitHub first"}, status_code=400)
        _schedule(app, background, "github", found["tenant_id"])
        return {"owner": owner, "repo": repo}

    @app.post("/setup/open")
    async def setup_open(request: Request):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        tenant = found["tenant_id"]
        status = _status(app, tenant)
        ready = [card for card in status["connectors"] if card["id"] in _CONNECTABLE and card["status"] in {"connected", "syncing"}]
        if not ready:
            return JSONResponse({"error": "connect a tool first"}, status_code=400)
        email = found["email"]
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
        found = _session(app, request)
        if found is None:
            return RedirectResponse("/login", status_code=303)
        if provider not in _CONNECTABLE:
            return JSONResponse({"error": "not available"}, status_code=404)
        ready, reason = configured(provider)
        if not ready:
            return JSONResponse({"error": reason}, status_code=404)
        try:
            url = begin(provider, found["tenant_id"], _redirect(request, provider), app.state.oauth_states)
        except Exception as exc:
            log.warning("connect did not start (%s)", type(exc).__name__)
            return JSONResponse({"error": "connect did not start"}, status_code=400)
        return RedirectResponse(url, status_code=302)

    @app.get("/connect/{provider}/callback")
    def connect_callback(provider: str, request: Request, background: BackgroundTasks):
        found = _session(app, request)
        if found is None:
            return RedirectResponse("/login", status_code=303)
        if provider not in _CONNECTABLE:
            return JSONResponse({"error": "not available"}, status_code=404)
        query = dict(request.query_params)
        try:
            expected = app.state.oauth_states.peek(query.get("state") or "", provider)
        except Exception:
            return RedirectResponse("/setup?error=state", status_code=303)
        if expected != found["tenant_id"]:
            return RedirectResponse("/setup?error=state", status_code=303)
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
            return RedirectResponse("/setup?error=state", status_code=302)
        except Exception as exc:
            log.warning("connect callback failed (%s)", type(exc).__name__)
            return RedirectResponse("/setup?error=connect", status_code=302)
        _schedule(app, background, provider, found["tenant_id"])
        extra = "&pick=folder" if provider == "google" else ""
        return RedirectResponse(f"/setup?connected={provider}{extra}", status_code=302)


def run_poll(provider: str, tenant: str) -> None:
    """One immediate sync after a connect. Connector errors propagate to the caller."""

    if provider == "slack":
        from scripts.run_worker import _slack_once

        _slack_once(tenant)
    elif provider == "notion":
        from scripts.run_worker import _notion_once

        _notion_once(tenant)
    elif provider == "google":
        from scripts.run_worker import _drive_once

        _drive_once(tenant)
    elif provider == "github":
        from scripts.run_worker import _github_once

        _github_once(tenant)


def _noop_poll(provider: str, tenant: str) -> None:
    return None


def _schedule(app, background: BackgroundTasks, provider: str, tenant: str | None = None) -> None:
    chosen = tenant or _tenant(app)

    def job() -> None:
        store = _store(app)
        try:
            app.state.poll_now(provider, chosen)
            store.update_status(chosen, provider, "connected")
        except ModuleNotFoundError as exc:
            # This process has no embedding model. The grant is saved; the worker syncs it.
            log.warning("connect poll skipped (%s)", type(exc).__name__)
            try:
                store.update_status(chosen, provider, "connected")
            except Exception:
                pass
        except Exception as exc:
            log.warning("connect poll failed (%s)", type(exc).__name__)
            try:
                store.update_status(chosen, provider, "failed")
            except Exception:
                pass

    background.add_task(job)


def _status(app, tenant: str | None = None) -> dict:
    tenant = tenant or _tenant(app)
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
        "tenant": tenant,
        "company": str((profile or {}).get("name") or ""),
        "model": "saved" if groq and groq.get("token") else "default",
        "connectors": cards,
    }


def _tenant(app, request: Request | None = None) -> str:
    if request is not None:
        found = _session(app, request)
        if found and found.get("tenant_id"):
            return found["tenant_id"]
    return (app.state.tenant_id or os.environ.get("WEAVE_TENANT_ID") or os.environ.get("WEAVE_TENANT") or "real").strip()


def _session(app, request: Request) -> dict | None:
    raw = request.cookies.get(SESSION_COOKIE, "")
    if not raw:
        return None
    try:
        return app.state.accounts.read_session(raw)
    except Exception:
        return None


def _cookie(response: RedirectResponse, request: Request, token: str) -> None:
    response.set_cookie(
        SESSION_COOKIE,
        token,
        max_age=SESSION_MAX_AGE,
        httponly=True,
        secure=request.url.scheme == "https",
        samesite="lax",
        path="/",
    )


def _form(body: bytes) -> dict[str, str]:
    parsed = urllib.parse.parse_qs(body.decode("utf-8", errors="replace"), keep_blank_values=True)
    return {key: values[-1] if values else "" for key, values in parsed.items()}


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


def _put(app, provider: str, external_id: str, payload: dict, *, status: str, tenant: str | None = None) -> None:
    _store(app).put(tenant or _tenant(app), provider, external_id, payload, status=status)


def _merge(app, provider: str, tenant: str | None = None, **fields) -> bool:
    store = _store(app)
    tenant = tenant or _tenant(app)
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
