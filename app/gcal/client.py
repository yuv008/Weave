"""Google Calendar API client for one allow-listed calendar."""

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path

_MAX_ATTEMPTS = 5
_API = "https://www.googleapis.com/calendar/v3"
_READONLY = "https://www.googleapis.com/auth/calendar.readonly"

Transport = Callable[[str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class CalendarRequestError(Exception):
    def __init__(self, status: int, error: str):
        self.status = status
        self.error = error
        super().__init__(f"Calendar request failed ({status}): {error}")


class CalendarClient:
    def __init__(
        self,
        calendar_id: str,
        *,
        credentials_file: str | None = None,
        credentials=None,
        token: str | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
    ):
        if not calendar_id:
            raise ValueError("calendar id is required")
        self.calendar_id = calendar_id
        if credentials is not None:
            self._token_value = ""
            self._credentials = credentials
        elif token:
            self._token_value = token
            self._credentials = None
        elif credentials_file:
            self._token_value = ""
            self._credentials = _load_credentials(credentials_file)
        else:
            raise ValueError("Calendar credentials are required")
        self._transport = transport or _urlopen_transport
        self._sleep = sleep or time.sleep
        self._random = random_fn or random.random

    def list_events(self, sync_token: str | None = None, page_token: str | None = None) -> dict:
        params: dict[str, str] = {"singleEvents": "true", "maxResults": "250"}
        if sync_token:
            params["syncToken"] = sync_token
        else:
            params["showDeleted"] = "true"
        if page_token:
            params["pageToken"] = page_token
        cal = urllib.parse.quote(self.calendar_id, safe="")
        return self._call("GET", f"/calendars/{cal}/events", params)

    def _bearer(self) -> str:
        if self._credentials is not None:
            from google.auth.transport.requests import Request

            self._credentials.refresh(Request())
            return self._credentials.token
        return self._token_value

    def _call(self, method: str, path: str, params: dict[str, str]) -> dict:
        del method
        query = urllib.parse.urlencode(params)
        url = f"{_API}{path}?{query}"
        headers = {"Authorization": f"Bearer {self._bearer()}"}
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                status, _hdrs, raw = self._transport(url, headers, None)
            except OSError as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                self._sleep(self._backoff(attempt))
                continue
            if status == 429 or status >= 500:
                last_error = CalendarRequestError(status, "retryable")
                if attempt == _MAX_ATTEMPTS:
                    raise last_error
                self._sleep(self._backoff(attempt))
                continue
            if status >= 400:
                raise CalendarRequestError(status, _api_error(raw))
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise CalendarRequestError(status, "invalid_json") from exc
            if not isinstance(parsed, dict):
                raise CalendarRequestError(status, "invalid_json")
            return parsed
        raise last_error or CalendarRequestError(0, "failed")

    def _backoff(self, attempt: int) -> float:
        return 0.5 * (2 ** (attempt - 1)) * (0.5 + self._random())


def calendar_client_from_env() -> CalendarClient:
    calendar_id = os.environ.get("GCAL_CALENDAR_ID", "").strip() or "primary"
    oauth = _oauth_credentials()
    if oauth is not None:
        return CalendarClient(calendar_id, credentials=oauth)
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or "secrets/gdrive-service-account.json"
    return CalendarClient(calendar_id, credentials_file=path)


def _oauth_credentials():
    path = os.environ.get("GOOGLE_OAUTH_TOKEN_FILE", "").strip()
    if not path or not Path(path).is_file():
        return None
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_file(path)


def _load_credentials(path: str):
    from google.oauth2 import service_account

    return service_account.Credentials.from_service_account_file(path, scopes=[_READONLY])


def _urlopen_transport(url: str, headers: dict[str, str], body: bytes | None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _api_error(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "http_error"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, dict) and isinstance(error.get("message"), str):
        return error["message"][:96]
    return "http_error"
