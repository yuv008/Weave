"""Read connector credentials. A stored row wins; otherwise the process environment is used."""

import os
import time
import urllib.parse
import urllib.request

from app.connect.secrets import MemorySecretStore, PostgresSecretStore

_store = None


def use_store(store) -> None:
    """Install the store for this process. ``None`` clears it."""

    global _store
    _store = store


def current_store():
    global _store
    if _store is not None:
        return _store
    if os.environ.get("DATABASE_URL"):
        _store = PostgresSecretStore()
        return _store
    _store = MemorySecretStore()
    return _store


def _tenant(tenant: str | None) -> str:
    return (tenant or os.environ.get("WEAVE_TENANT_ID") or os.environ.get("WEAVE_TENANT") or "").strip()


def _explicit(tenant: str | None) -> str:
    """A caller-chosen tenant. Empty means the process environment may be used."""

    if tenant is None:
        return ""
    return tenant.strip()


def row(tenant: str | None, provider: str) -> dict | None:
    name = _tenant(tenant)
    if not name:
        return None
    try:
        return current_store().get(name, provider)
    except Exception:
        return None


def slack_bot_token(tenant: str | None = None) -> str:
    found = row(tenant, "slack")
    if found and found.get("token") and found.get("status") != "failed":
        return str(found["token"])
    if _explicit(tenant):
        return ""
    return os.environ.get("SLACK_BOT_TOKEN", "").strip()


def slack_workspace_id(tenant: str | None = None) -> str:
    found = row(tenant, "slack")
    if found and found.get("external_id"):
        return str(found["external_id"])
    if _explicit(tenant):
        return ""
    return os.environ.get("WEAVE_WORKSPACE_ID", "").strip()


def notion_token(tenant: str | None = None) -> str:
    found = row(tenant, "notion")
    if found and found.get("token") and found.get("status") != "failed":
        return str(found["token"])
    if _explicit(tenant):
        return ""
    return os.environ.get("NOTION_TOKEN", "").strip()


def notion_workspace_id(tenant: str | None = None) -> str:
    found = row(tenant, "notion")
    if found and found.get("external_id"):
        return str(found["external_id"])
    if _explicit(tenant):
        return ""
    return os.environ.get("NOTION_WORKSPACE_ID", "").strip()


def google_folder_id(tenant: str | None = None) -> str:
    found = row(tenant, "google")
    folder = str((found or {}).get("folder_id") or "").strip()
    if folder:
        return folder
    # A different company must name its own folder. The curated tenant
    # keeps the service-account folder from the process environment.
    explicit = _explicit(tenant)
    home = (os.environ.get("WEAVE_TENANT_ID") or os.environ.get("WEAVE_TENANT") or "").strip()
    if explicit and explicit != home:
        return ""
    return os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()


def google_uses_service_account(tenant: str | None = None) -> bool:
    """Read the curated folder with the service account when OAuth cannot."""

    if google_access_token(tenant):
        return False
    found = row(tenant, "google")
    grant_folder = str((found or {}).get("folder_id") or "").strip()
    env_folder = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if grant_folder != env_folder and grant_folder:
        return False
    explicit = _explicit(tenant)
    home = (os.environ.get("WEAVE_TENANT_ID") or os.environ.get("WEAVE_TENANT") or "").strip()
    if explicit and explicit != home:
        return False
    return bool(google_folder_id(tenant))


def google_access_token(tenant: str | None = None) -> str:
    """A Drive access token from the stored grant, refreshed when it is due."""

    found = row(tenant, "google")
    if not found or found.get("status") == "failed":
        return ""
    token = str(found.get("token") or "")
    expires = float(found.get("expires_at") or 0)
    refresh = str(found.get("refresh_token") or "")
    if token and (not expires or expires > time.time() + 60):
        return token
    # An expired bearer is rejected by Drive. Callers can use the service
    # account only when this returns empty.
    if not refresh:
        return ""
    client_id = os.environ.get("GOOGLE_CLIENT_ID", "").strip()
    client_secret = os.environ.get("GOOGLE_CLIENT_SECRET", "").strip()
    if not client_id or not client_secret:
        return ""
    body = urllib.parse.urlencode({
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh,
        "grant_type": "refresh_token",
    }).encode()
    request = urllib.request.Request("https://oauth2.googleapis.com/token", data=body)
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = __import__("json").loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    fresh = str(payload.get("access_token") or "")
    if not fresh:
        return ""
    updated = dict(found)
    updated["token"] = fresh
    updated["expires_at"] = time.time() + int(payload.get("expires_in") or 3600)
    _save(tenant, "google", updated)
    return fresh


def github_coordinates(tenant: str | None = None) -> tuple[str, str]:
    found = row(tenant, "github") or {}
    owner = str(found.get("owner") or "").strip()
    repo = str(found.get("repo") or "").strip()
    if _explicit(tenant):
        return owner, repo
    return owner or os.environ.get("GITHUB_OWNER", "").strip(), repo or os.environ.get("GITHUB_REPO", "").strip()


def github_access_token(tenant: str | None = None) -> str:
    found = row(tenant, "github")
    if found and found.get("status") != "failed":
        token = str(found.get("token") or "")
        expires = float(found.get("expires_at") or 0)
        if token and (not expires or expires > time.time() + 60):
            return token
        installation = str(found.get("installation_id") or found.get("external_id") or "")
        minted = _github_installation_token(installation) if installation else ""
        if minted:
            updated = dict(found)
            updated["token"] = minted
            updated["expires_at"] = time.time() + 3000
            _save(tenant, "github", updated)
            return minted
        if token:
            return token
    if _explicit(tenant):
        return ""
    return os.environ.get("GITHUB_TOKEN", "").strip()


def _save(tenant: str | None, provider: str, payload: dict) -> None:
    name = _tenant(tenant)
    if not name:
        return
    external = str(payload.get("external_id") or "")
    body = {key: value for key, value in payload.items() if key not in {"external_id", "scopes", "status"}}
    try:
        current_store().put(
            name, provider, external, body,
            scopes=str(payload.get("scopes") or ""),
            status=str(payload.get("status") or "connected"),
        )
    except Exception:
        return


def _github_installation_token(installation_id: str) -> str:
    app_id = os.environ.get("GITHUB_APP_ID", "").strip()
    pem = os.environ.get("GITHUB_APP_PRIVATE_KEY", "").replace("\\n", "\n").strip()
    if not installation_id or not app_id or not pem:
        return ""
    try:
        assertion = _github_app_jwt(app_id, pem)
        request = urllib.request.Request(
            f"https://api.github.com/app/installations/{installation_id}/access_tokens",
            data=b"",
            headers={
                "Authorization": f"Bearer {assertion}",
                "Accept": "application/vnd.github+json",
                "User-Agent": "weave",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = __import__("json").loads(response.read().decode("utf-8"))
    except Exception:
        return ""
    return str(payload.get("token") or "")


def _github_app_jwt(app_id: str, pem: str) -> str:
    import json
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import padding

    def pack(value: dict) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64_url(raw)

    now = int(time.time())
    signing = f"{pack({'alg': 'RS256', 'typ': 'JWT'})}.{pack({'iat': now - 60, 'exp': now + 540, 'iss': app_id})}"
    key = serialization.load_pem_private_key(pem.encode("utf-8"), password=None)
    signature = key.sign(signing.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
    return f"{signing}.{base64_url(signature)}"


def base64_url(raw: bytes) -> str:
    import base64

    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
