"""Setup page and OAuth routes on the webhook app."""

import json
import logging
import os
import re
import secrets
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from fastapi import BackgroundTasks, FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse

from app.connect.oauth import CARDS, MemoryStateStore, StateMismatch, begin, complete, configured
from app.connect.mailer import send_quietly
from app.connect.setup_code import SetupCodes

log = logging.getLogger("weave.connect")
PAGE = Path(__file__).resolve().parents[2] / "frontend" / "index.html"
LANDING = PAGE.with_name("landing.html")
LOGIN = PAGE.with_name("login.html")
SIGNUP = PAGE.with_name("signup.html")
FORGOT = PAGE.with_name("forgot.html")
RESET = PAGE.with_name("reset.html")
INSTALLER = PAGE.parent / "downloads" / "WeavePetSetup.exe"
BRAND = {"mark.png", "icon.png", "wisp.svg"}
SESSION_COOKIE = "weave_session"
SESSION_MAX_AGE = 14 * 24 * 3600
LANDING_FILES = {"stage.js": "text/javascript", "stage.css": "text/css"}
_CONNECTABLE = {"slack", "notion", "google", "github"}
TOKEN_DAYS = 30


def register_connect(app: FastAPI) -> None:
    app.state.oauth_states = MemoryStateStore()
    app.state.setup_codes = SetupCodes()
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
    async def signup_submit(request: Request, background: BackgroundTasks):
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
        if found.get("verified") is False:
            _send_verify(app, request, background, found)
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
            "verified": found.get("verified") is not False,
            "role": found.get("role") or "",
            "memberships": found["memberships"],
        }

    @app.post("/logout")
    def logout(request: Request):
        raw = request.cookies.get(SESSION_COOKIE, "")
        if raw:
            try:
                app.state.accounts.logout(raw)
            except Exception as exc:
                log.warning("session was not ended (%s)", type(exc).__name__)
        response = RedirectResponse("/login", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/forgot")
    def forgot_page():
        if not FORGOT.is_file():
            return JSONResponse({"error": "page is missing"}, status_code=404)
        return FileResponse(FORGOT)

    @app.post("/forgot")
    async def forgot_submit(request: Request, background: BackgroundTasks):
        email = _form(await request.body()).get("email", "")
        try:
            raw = app.state.accounts.start_reset(email)
        except Exception as exc:
            log.warning("reset link was not made (%s)", type(exc).__name__)
            raw = None
        if raw:
            link = _link(request, "/reset", raw)
            background.add_task(
                send_quietly, app.state.mailer, email.strip().lower(), "Reset your Weave password",
                "Someone asked to reset the password for this Weave account.\n\n"
                f"Choose a new password here:\n{link}\n\n"
                "The link works once, for 30 minutes. If you did not ask, ignore this email; "
                "your password stays the same.\n",
            )
        # The same answer whether or not the address has an account.
        return RedirectResponse("/forgot?sent=1", status_code=303)

    @app.get("/reset")
    def reset_page():
        if not RESET.is_file():
            return JSONResponse({"error": "page is missing"}, status_code=404)
        return FileResponse(RESET)

    @app.post("/reset")
    async def reset_submit(request: Request):
        from app.connect.accounts import AccountError

        fields = _form(await request.body())
        token = fields.get("token", "")
        try:
            done = app.state.accounts.finish_reset(token, fields.get("password", ""))
        except AccountError:
            query = urllib.parse.urlencode({"token": token, "error": "password"})
            return RedirectResponse(f"/reset?{query}", status_code=303)
        if not done:
            return RedirectResponse("/reset?error=expired", status_code=303)
        response = RedirectResponse("/login?reset=1", status_code=303)
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    @app.get("/verify")
    def verify_email(token: str = ""):
        try:
            done = app.state.accounts.verify(token)
        except Exception as exc:
            log.warning("address was not verified (%s)", type(exc).__name__)
            done = False
        return RedirectResponse("/setup?verified=1" if done else "/setup?verified=expired", status_code=303)

    @app.post("/verify/resend")
    def verify_resend(request: Request, background: BackgroundTasks):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        if found.get("verified") is not False:
            return {"verified": True}
        try:
            _send_verify(app, request, background, found)
        except Exception as exc:
            log.warning("verify link was not made (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not send the email"}, status_code=503)
        return {"sent": True}

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

    @app.get("/downloads/WeavePetSetup.exe")
    def pet_installer():
        if not INSTALLER.is_file():
            return JSONResponse({"error": "the Windows pet installer is not on this server yet"}, status_code=404)
        return FileResponse(INSTALLER, media_type="application/vnd.microsoft.portable-executable", filename="WeavePetSetup.exe")

    @app.get("/guide")
    def pet_guide():
        path = PAGE.parent / "guide.html"
        if path.is_file():
            page = FileResponse(path)
            page.headers["Cache-Control"] = "no-store"
            return page
        return JSONResponse({"error": "the pet walkthrough is missing"}, status_code=404)

    @app.get("/guide/media/{name}")
    def guide_media(name: str):
        if re.fullmatch(r"[a-z0-9-]+\.png", name) is None:
            return JSONResponse({"error": "not found"}, status_code=404)
        root = (PAGE.parent / "guide-media").resolve()
        path = (root / name).resolve()
        if path.parent != root or not path.is_file():
            return JSONResponse({"error": "not found"}, status_code=404)
        return FileResponse(path, media_type="image/png")

    @app.get("/setup")
    def setup_page(request: Request):
        if _session(app, request) is None:
            return RedirectResponse("/login", status_code=303)
        if PAGE.is_file():
            page = FileResponse(PAGE)
            page.headers["Cache-Control"] = "no-store"
            return page
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
        denied = _deny(found)
        if denied is not None:
            return denied
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        tenant = found["tenant_id"]
        if body.get("remove") is True:
            try:
                _store(app).delete(tenant, "groq")
            except Exception as exc:
                log.warning("model key was not removed (%s)", type(exc).__name__)
                return JSONResponse({"error": "could not remove the model key"}, status_code=503)
            return {"model": "shared"}
        key = str(body.get("key") or "").strip()
        if not key:
            return JSONResponse({"error": "paste a Groq key"}, status_code=400)
        if len(key) > 400:
            return JSONResponse({"error": "that key is too long"}, status_code=400)
        checker = getattr(app.state, "groq_check", None) or _groq_key_works
        try:
            works = checker(key)
        except Exception as exc:
            log.warning("model key was not checked (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not reach Groq; try again"}, status_code=503)
        if not works:
            return JSONResponse({"error": "Groq rejected that key"}, status_code=400)
        try:
            _put(app, "groq", "groq", {"token": key}, status="connected", tenant=tenant)
        except Exception as exc:
            log.warning("model key was not saved (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not save the model key"}, status_code=503)
        return {"model": "own"}

    @app.post("/setup/google/folder")
    async def setup_folder(request: Request, background: BackgroundTasks):
        found = _session(app, request)
        denied = _deny(found)
        if denied is not None:
            return denied
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
        denied = _deny(found)
        if denied is not None:
            return denied
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
        denied = _deny(found)
        if denied is not None:
            return denied
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

    @app.post("/setup/install-code")
    async def setup_install_code(request: Request):
        found = _session(app, request)
        denied = _deny(found)
        if denied is not None:
            return denied
        tenant = found["tenant_id"]
        status = _status(app, tenant)
        ready = [card for card in status["connectors"] if card["id"] in _CONNECTABLE and card["status"] in {"connected", "syncing"}]
        if not ready:
            return JSONResponse({"error": "connect a tool first"}, status_code=400)
        email = found["email"]
        if not email:
            return JSONResponse({"error": "the server has no viewer email"}, status_code=400)
        try:
            token = _mint(tenant, email, app, label="pet")
        except Exception as exc:
            log.warning("viewer token was not minted (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not create a viewer token"}, status_code=500)
        agent = os.environ.get("WEAVE_AGENT_URL", "http://127.0.0.1:8765").rstrip("/")
        code = secrets.token_urlsafe(18)
        app.state.setup_codes.put(
            code,
            {"cue_url": f"{agent}/wisp/cue", "token": token, "mcp_url": f"{agent}/mcp"},
        )
        return {"code": code, "filename": f"WeavePetSetup-{code}.exe"}

    @app.get("/setup/tokens")
    def setup_tokens(request: Request):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        try:
            return {"days": TOKEN_DAYS, "tokens": _own_tokens(app, found["tenant_id"], found["email"])}
        except Exception as exc:
            log.warning("tokens were not listed (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not list tokens"}, status_code=503)

    @app.post("/setup/tokens/revoke")
    async def setup_tokens_revoke(request: Request):
        found = _session(app, request)
        if found is None:
            return JSONResponse({"error": "sign in"}, status_code=401)
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        short = None if body.get("all") is True else str(body.get("id") or "")
        if short is not None and re.fullmatch(r"[0-9a-f]{8}", short) is None:
            return JSONResponse({"error": "pick a token to revoke"}, status_code=400)
        try:
            revoked = _revoke_tokens(app, found["tenant_id"], found["email"], short)
        except Exception as exc:
            log.warning("tokens were not revoked (%s)", type(exc).__name__)
            return JSONResponse({"error": "could not revoke"}, status_code=503)
        return {"revoked": revoked}

    @app.post("/setup/install/redeem")
    async def setup_install_redeem(request: Request):
        body = await _body(request)
        if isinstance(body, JSONResponse):
            return body
        code = str(body.get("code") or "")
        if re.fullmatch(r"[A-Za-z0-9_-]{16,80}", code) is None:
            return JSONResponse({"error": "that setup code has expired"}, status_code=410)
        payload = app.state.setup_codes.take(code)
        if payload is None:
            return JSONResponse({"error": "that setup code has expired"}, status_code=410)
        return {"cue_url": payload["cue_url"], "token": payload["token"], "mcp_url": payload["mcp_url"]}

    @app.get("/connect/{provider}")
    def connect_start(provider: str, request: Request):
        found = _session(app, request)
        if found is None:
            return RedirectResponse("/login", status_code=303)
        if _deny(found) is not None:
            return RedirectResponse("/setup?error=verify", status_code=303)
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


def run_poll(provider: str, tenant: str) -> bool | None:
    """One immediate sync after a connect. Connector errors propagate to the caller."""

    if provider == "slack":
        from scripts.run_worker import _slack_once

        _slack_once(tenant)
    elif provider == "notion":
        from scripts.run_worker import _notion_once

        _notion_once(tenant)
    elif provider == "google":
        from app.connect.credentials import google_folder_id
        from scripts.run_worker import _drive_once

        if _drive_once(tenant) or not google_folder_id(tenant):
            return None
        return False
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
            if app.state.poll_now(provider, chosen) is False:
                return
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
    if groq and groq.get("token"):
        model = "failed" if groq.get("status") == "failed" else "own"
    else:
        model = "shared"
    return {
        "tenant": tenant,
        "company": str((profile or {}).get("name") or ""),
        "model": model,
        "shared_used_today": _shared_used_today(app, tenant) if model != "own" else None,
        "drive_tester_email": os.environ.get("WEAVE_VIEWER_EMAIL", "").strip(),
        "connectors": cards,
    }


def _shared_used_today(app, tenant: str) -> int | None:
    """Requests this company made on the server key today, or ``None`` when it cannot be read."""

    if getattr(app.state, "pg_connect", None) is None and not getattr(app.state, "live_db", False):
        return None
    try:
        conn = _pg(app)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT calls FROM model_usage WHERE tenant_id = %s AND day = current_date", (tenant,))
                row = cur.fetchone()
            conn.commit()
        finally:
            conn.close()
    except Exception:
        return None
    return int(row[0]) if row else 0


def _groq_key_works(key: str) -> bool:
    """True when Groq accepts ``key``. 401 and 403 are a rejected key; other failures raise."""

    request = urllib.request.Request(
        "https://api.groq.com/openai/v1/models",
        headers={"Authorization": f"Bearer {key}", "User-Agent": "weave-setup"},
    )
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            return response.status == 200
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            return False
        raise


def _tenant(app, request: Request | None = None) -> str:
    if request is not None:
        found = _session(app, request)
        if found and found.get("tenant_id"):
            return found["tenant_id"]
    return (app.state.tenant_id or os.environ.get("WEAVE_TENANT_ID") or os.environ.get("WEAVE_TENANT") or "real").strip()


def _deny(found: dict | None, *, owner: bool = False) -> JSONResponse | None:
    """401 without a session, 403 before the address is verified or, with ``owner``, for a member."""

    if found is None:
        return JSONResponse({"error": "sign in"}, status_code=401)
    if found.get("verified") is False:
        return JSONResponse({"error": "confirm your email first", "needs": "verify"}, status_code=403)
    if owner and found.get("role") != "owner":
        return JSONResponse({"error": "only the company owner can do this"}, status_code=403)
    return None


def _link(request: Request, path: str, token: str) -> str:
    base = os.environ.get("WEAVE_PUBLIC_URL", str(request.base_url)).rstrip("/")
    return f"{base}{path}?" + urllib.parse.urlencode({"token": token})


def _send_verify(app, request: Request, background: BackgroundTasks, found: dict) -> None:
    raw = app.state.accounts.start_verify(found["user_id"])
    link = _link(request, "/verify", raw)
    background.add_task(
        send_quietly, app.state.mailer, found["email"], "Confirm your email for Weave",
        f"Hi {found['name']},\n\n"
        f"Confirm this address to finish setting up Weave:\n{link}\n\n"
        "The link works for 24 hours. If you did not sign up for Weave, ignore this email.\n",
    )


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


def _pg(app):
    connect = app.state.pg_connect if getattr(app.state, "pg_connect", None) else None
    if connect is None:
        from shared.pg import connect_pg

        connect = connect_pg
    return connect()


def _mint(tenant: str, email: str, app, label: str = "mcp") -> str:
    from agents.gate.resolve import hash_token
    from shared.ids import person_id

    token = f"wv_{secrets.token_urlsafe(24)}"
    conn = _pg(app)
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO api_token (token_sha256, tenant_id, person_id, label, expires_at) "
                f"VALUES (%s, %s, %s, %s, now() + interval '{TOKEN_DAYS} days')",
                (hash_token(token), tenant, person_id(tenant, email), label),
            )
        conn.commit()
    finally:
        conn.close()
    return token


def _own_tokens(app, tenant: str, email: str) -> list[dict]:
    """The signed-in person's tokens. The id is a hash prefix; the hash itself never leaves."""

    from shared.ids import person_id

    conn = _pg(app)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT left(token_sha256, 8), label, created_at, expires_at, revoked_at
                FROM api_token WHERE tenant_id = %s AND person_id = %s
                ORDER BY created_at DESC LIMIT 50
                """,
                (tenant, person_id(tenant, email)),
            )
            rows = cur.fetchall()
        conn.commit()
    finally:
        conn.close()
    stamp = lambda value: value.isoformat() if value else None
    return [
        {"id": short, "label": label or "", "created": stamp(created), "expires": stamp(expires), "revoked": stamp(revoked)}
        for short, label, created, expires, revoked in rows
    ]


def _revoke_tokens(app, tenant: str, email: str, short: str | None) -> int:
    """Revoke one token by its id, or all of them when ``short`` is ``None``. Only the caller's own."""

    from shared.ids import person_id

    sql = "UPDATE api_token SET revoked_at = now() WHERE tenant_id = %s AND person_id = %s AND revoked_at IS NULL"
    params: tuple = (tenant, person_id(tenant, email))
    if short is not None:
        sql += " AND left(token_sha256, 8) = %s"
        params += (short,)
    conn = _pg(app)
    try:
        with conn.cursor() as cur:
            cur.execute(sql, params)
            count = cur.rowcount
        conn.commit()
    finally:
        conn.close()
    return max(count or 0, 0)
