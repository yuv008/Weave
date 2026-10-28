"""OAuth start and callback. State is single-use and checked against the provider."""

import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request

SLACK_SCOPES = ",".join((
    "channels:history",
    "channels:read",
    "channels:join",
    "chat:write",
    "app_mentions:read",
    "im:history",
    "im:read",
    "im:write",
    "users:read",
    "users:read.email",
))
GOOGLE_SCOPE = "https://www.googleapis.com/auth/drive.readonly"
CARDS = (
    ("slack", "Slack"),
    ("notion", "Notion"),
    ("google", "Google Drive"),
    ("github", "GitHub"),
    ("hubspot", "HubSpot"),
)


class StateMismatch(ValueError):
    """The callback state does not match the connect that was started."""


class MemoryStateStore:
    def __init__(self):
        self._rows: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()

    def put(self, state: str, tenant: str, provider: str, ttl: int = 600) -> None:
        with self._lock:
            self._rows[state] = (tenant, provider, time.time() + ttl)

    def peek(self, state: str, provider: str) -> str:
        with self._lock:
            row = self._rows.get(state)
        if row is None or row[1] != provider or row[2] < time.time():
            raise StateMismatch("state")
        return row[0]

    def pop(self, state: str, provider: str) -> str:
        with self._lock:
            row = self._rows.pop(state, None)
        if row is None or row[1] != provider or row[2] < time.time():
            raise StateMismatch("state")
        return row[0]


def configured(provider: str) -> tuple[bool, str]:
    """Whether the Connect button can start, and the reason it cannot."""

    if provider == "hubspot":
        return False, "Connect is not available for HubSpot yet."
    if provider == "slack":
        if os.environ.get("SLACK_CLIENT_ID", "").strip() and os.environ.get("SLACK_CLIENT_SECRET", "").strip():
            return True, ""
        return False, "The Slack app is not set on the server."
    if provider == "notion":
        if os.environ.get("NOTION_CLIENT_ID", "").strip() and os.environ.get("NOTION_CLIENT_SECRET", "").strip():
            return True, ""
        return False, "The Notion integration is not set on the server."
    if provider == "google":
        if os.environ.get("GOOGLE_CLIENT_ID", "").strip() and os.environ.get("GOOGLE_CLIENT_SECRET", "").strip():
            return True, ""
        return False, "The Google app is not set on the server."
    if provider == "github":
        # The client id and secret prove which installs the signed-in user can reach.
        names = ("GITHUB_APP_SLUG", "GITHUB_APP_CLIENT_ID", "GITHUB_APP_CLIENT_SECRET")
        if all(os.environ.get(name, "").strip() for name in names):
            return True, ""
        return False, "The GitHub App is not set on the server."
    return False, "This connector is not available."


def begin(provider: str, tenant: str, redirect_uri: str, states: MemoryStateStore) -> str:
    ready, reason = configured(provider)
    if not ready:
        raise RuntimeError(reason)
    state = secrets.token_urlsafe(24)
    states.put(state, tenant, provider)
    if provider == "slack":
        query = urllib.parse.urlencode({
            "client_id": os.environ["SLACK_CLIENT_ID"].strip(),
            "scope": SLACK_SCOPES,
            "redirect_uri": redirect_uri,
            "state": state,
        })
        return f"https://slack.com/oauth/v2/authorize?{query}"
    if provider == "notion":
        query = urllib.parse.urlencode({
            "client_id": os.environ["NOTION_CLIENT_ID"].strip(),
            "response_type": "code",
            "owner": "user",
            "redirect_uri": redirect_uri,
            "state": state,
        })
        return f"https://api.notion.com/v1/oauth/authorize?{query}"
    if provider == "google":
        query = urllib.parse.urlencode({
            "client_id": os.environ["GOOGLE_CLIENT_ID"].strip(),
            "redirect_uri": redirect_uri,
            "response_type": "code",
            "scope": GOOGLE_SCOPE,
            "access_type": "offline",
            "prompt": "consent",
            "state": state,
        })
        return f"https://accounts.google.com/o/oauth2/v2/auth?{query}"
    if provider == "github":
        query = urllib.parse.urlencode({"state": state})
        return f"https://github.com/apps/{os.environ['GITHUB_APP_SLUG'].strip()}/installations/new?{query}"
    raise RuntimeError("This connector is not available.")


def complete(provider: str, state: str, query: dict, states: MemoryStateStore, store, *, redirect_uri: str, http=None) -> str:
    """Exchange the callback and store the credential. Returns the tenant id."""

    tenant = states.pop(state or "", provider)
    payload, external, scopes = exchange(provider, query, redirect_uri, http=http)
    if provider == "github" and store.find_tenant("github", external) not in (None, tenant):
        raise ValueError("that GitHub install belongs to another company")
    previous = None
    try:
        previous = store.get(tenant, provider)
    except Exception:
        previous = None
    if previous:
        for key, value in previous.items():
            if key not in payload and key not in {"status", "scopes"} and value:
                payload.setdefault(key, value)
        if previous.get("external_id") and not external:
            external = str(previous["external_id"])
    store.put(tenant, provider, external, payload, scopes=scopes, status="syncing")
    return tenant


def exchange(provider: str, query: dict, redirect_uri: str, *, http=None) -> tuple[dict, str, str]:
    if provider == "slack":
        payload = _form("https://slack.com/api/oauth.v2.access", {
            "client_id": os.environ.get("SLACK_CLIENT_ID", "").strip(),
            "client_secret": os.environ.get("SLACK_CLIENT_SECRET", "").strip(),
            "code": query.get("code") or "",
            "redirect_uri": redirect_uri,
        }, http=http)
        token = str(payload.get("access_token") or "")
        if not token or payload.get("ok") is False:
            raise ValueError("slack")
        team = payload.get("team") if isinstance(payload.get("team"), dict) else {}
        return {"token": token}, str(team.get("id") or ""), SLACK_SCOPES
    if provider == "notion":
        basic = _basic(os.environ.get("NOTION_CLIENT_ID", ""), os.environ.get("NOTION_CLIENT_SECRET", ""))
        payload = _json("https://api.notion.com/v1/oauth/token", {
            "grant_type": "authorization_code",
            "code": query.get("code") or "",
            "redirect_uri": redirect_uri,
        }, headers={"Authorization": f"Basic {basic}", "Notion-Version": "2022-06-28"}, http=http)
        token = str(payload.get("access_token") or "")
        if not token:
            raise ValueError("notion")
        return {"token": token}, str(payload.get("workspace_id") or ""), ""
    if provider == "google":
        payload = _form("https://oauth2.googleapis.com/token", {
            "code": query.get("code") or "",
            "client_id": os.environ.get("GOOGLE_CLIENT_ID", "").strip(),
            "client_secret": os.environ.get("GOOGLE_CLIENT_SECRET", "").strip(),
            "redirect_uri": redirect_uri,
            "grant_type": "authorization_code",
        }, http=http)
        token = str(payload.get("access_token") or "")
        if not token:
            raise ValueError("google")
        expires = time.time() + int(payload.get("expires_in") or 3600)
        body = {"token": token, "expires_at": expires}
        if payload.get("refresh_token"):
            body["refresh_token"] = payload["refresh_token"]
        return body, "", GOOGLE_SCOPE
    if provider == "github":
        installation = str(query.get("installation_id") or "")
        code = str(query.get("code") or "")
        if not installation.isdigit() or not code:
            raise ValueError("github")
        # The browser chooses installation_id. Only an install the signed-in
        # GitHub user can reach is accepted.
        payload = _form("https://github.com/login/oauth/access_token", {
            "client_id": os.environ.get("GITHUB_APP_CLIENT_ID", "").strip(),
            "client_secret": os.environ.get("GITHUB_APP_CLIENT_SECRET", "").strip(),
            "code": code,
        }, headers={"Accept": "application/json"}, http=http)
        user_token = str(payload.get("access_token") or "")
        if not user_token or installation not in _github_user_installations(user_token, http=http):
            raise ValueError("github")
        return {"installation_id": installation}, installation, ""
    raise ValueError(provider)


def _github_user_installations(user_token: str, *, http=None) -> set[str]:
    headers = {
        "Authorization": f"Bearer {user_token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "weave",
    }
    found: set[str] = set()
    for page in range(1, 11):
        payload = _get(f"https://api.github.com/user/installations?per_page=100&page={page}", headers=headers, http=http)
        rows = payload.get("installations") if isinstance(payload.get("installations"), list) else []
        found.update(str(row.get("id")) for row in rows if isinstance(row, dict) and row.get("id"))
        if len(rows) < 100:
            break
    return found


def _get(url: str, *, headers: dict, http=None) -> dict:
    if http is not None:
        return http(url, None, headers)
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _form(url: str, fields: dict, *, headers: dict | None = None, http=None) -> dict:
    if http is not None:
        return http(url, fields)
    data = urllib.parse.urlencode(fields).encode()
    request = urllib.request.Request(url, data=data, headers=headers or {})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _json(url: str, fields: dict, *, headers: dict, http=None) -> dict:
    if http is not None:
        return http(url, fields, headers)
    data = json.dumps(fields).encode()
    request = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode("utf-8"))


def _basic(client_id: str, client_secret: str) -> str:
    import base64

    raw = f"{client_id.strip()}:{client_secret.strip()}".encode()
    return base64.b64encode(raw).decode("ascii")
