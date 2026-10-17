"""Google Drive client for one shared folder.

Credentials are a service-account JSON path. Tests inject ``token`` and
``transport`` and do not read a key file.
"""

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

_MAX_ATTEMPTS = 5
_API = "https://www.googleapis.com/drive/v3"
_READONLY = "https://www.googleapis.com/auth/drive.readonly"
DOC_MIME = "application/vnd.google-apps.document"

Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


class DriveRequestError(Exception):
    def __init__(self, status: int, error: str):
        self.status = status
        self.error = error
        super().__init__(f"Drive request failed ({status}): {error}")


class DriveClient:
    def __init__(
        self,
        *,
        credentials_file: str | None = None,
        token: str | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
    ):
        if token:
            self._token_value = token
            self._credentials = None
        elif credentials_file:
            self._token_value = ""
            self._credentials = _load_credentials(credentials_file)
        else:
            raise ValueError("Drive credentials are required")
        self._transport = transport or _urlopen_transport
        self._sleep = sleep or time.sleep
        self._random = random_fn or random.random

    def list_files(self, folder_id: str, page_token: str | None = None, modified_after: str | None = None) -> dict:
        if not folder_id or "'" in folder_id:
            raise ValueError("folder id is required")
        query = f"'{folder_id}' in parents"
        if modified_after:
            query += f" and modifiedTime > '{modified_after}'"
        params = {
            "q": query,
            "fields": "nextPageToken,files(id,mimeType,modifiedTime,trashed,name,owners(emailAddress,displayName))",
            "pageSize": "100",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        }
        if page_token:
            params["pageToken"] = page_token
        return self._call("GET", "/files", params)

    def export_document(self, file_id: str) -> str:
        payload = self._call(
            "GET",
            f"/files/{urllib.parse.quote(file_id, safe='')}/export",
            {"mimeType": "text/plain"},
            raw=True,
        )
        if isinstance(payload, bytes):
            return payload.decode("utf-8", errors="replace")
        return ""

    def get_file(self, file_id: str) -> dict:
        return self._call(
            "GET",
            f"/files/{urllib.parse.quote(file_id, safe='')}",
            {"fields": "id,mimeType,modifiedTime,trashed,name", "supportsAllDrives": "true"},
        )

    def _bearer(self) -> str:
        if self._credentials is not None:
            from google.auth.transport.requests import Request

            self._credentials.refresh(Request())
            return self._credentials.token
        return self._token_value

    def _call(self, method: str, path: str, params: dict[str, str], *, raw: bool = False):
        del method
        query = urllib.parse.urlencode(params)
        url = f"{_API}{path}?{query}"
        token = self._bearer()
        if token and token in url:
            raise RuntimeError("refusing to place the Drive token in the request URL")
        headers = {"Authorization": f"Bearer {token}"}
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                status, response_headers, body = self._transport(url, headers)
            except OSError as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                self._sleep(self._backoff(attempt))
                continue
            if status == 429 or status >= 500:
                last_error = DriveRequestError(status, "retryable")
                if attempt == _MAX_ATTEMPTS:
                    raise last_error
                self._sleep(self._backoff(attempt))
                continue
            if status >= 400:
                raise DriveRequestError(status, _drive_error(body))
            if raw:
                return body
            try:
                parsed = json.loads(body) if body else {}
            except json.JSONDecodeError as exc:
                raise DriveRequestError(status, "invalid_json") from exc
            if not isinstance(parsed, dict):
                raise DriveRequestError(status, "invalid_json")
            return parsed
        raise last_error or DriveRequestError(0, "failed")

    def _backoff(self, attempt: int) -> float:
        return 0.5 * (2 ** (attempt - 1)) * (0.5 + self._random())


def drive_client_from_env() -> DriveClient:
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or "secrets/gdrive-service-account.json"
    return DriveClient(credentials_file=path)


def _load_credentials(path: str):
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(path, scopes=[_READONLY])


def _urlopen_transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _drive_error(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "http_error"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("status"), str):
        return error["status"][:64]
    if isinstance(error, str):
        return error[:64]
    return "http_error"
