"""Gmail API client for one allow-listed mailbox."""

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
_API = "https://gmail.googleapis.com/gmail/v1"
_READONLY = "https://www.googleapis.com/auth/gmail.readonly"

Transport = Callable[[str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class GmailRequestError(Exception):
    def __init__(self, status: int, error: str):
        self.status = status
        self.error = error
        super().__init__(f"Gmail request failed ({status}): {error}")


class GmailClient:
    def __init__(
        self,
        mailbox: str,
        *,
        credentials_file: str | None = None,
        credentials=None,
        token: str | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
    ):
        if not mailbox:
            raise ValueError("mailbox is required")
        self.mailbox = mailbox
        if credentials is not None:
            self._token_value = ""
            self._credentials = credentials
        elif token:
            self._token_value = token
            self._credentials = None
        elif credentials_file:
            self._token_value = ""
            self._credentials = _load_credentials(credentials_file, mailbox)
        else:
            raise ValueError("Gmail credentials are required")
        self._transport = transport or _urlopen_transport
        self._sleep = sleep or time.sleep
        self._random = random_fn or random.random

    def list_messages(self, page_token: str | None = None, max_results: int = 100) -> dict:
        params = {"maxResults": str(max_results)}
        if page_token:
            params["pageToken"] = page_token
        return self._call("GET", f"/users/{_user(self.mailbox)}/messages", params)

    def get_message(self, message_id: str) -> dict:
        return self._call(
            "GET",
            f"/users/{_user(self.mailbox)}/messages/{urllib.parse.quote(message_id, safe='')}",
            {
                "format": "metadata",
                "metadataHeaders": ["From", "To", "Subject", "Date", "In-Reply-To"],
            },
        )

    def list_history(self, start_history_id: str, page_token: str | None = None) -> dict:
        params = {"startHistoryId": start_history_id, "historyTypes": "messageAdded,messageDeleted"}
        if page_token:
            params["pageToken"] = page_token
        return self._call("GET", f"/users/{_user(self.mailbox)}/history", params)

    def _bearer(self) -> str:
        if self._credentials is not None:
            from google.auth.transport.requests import Request

            self._credentials.refresh(Request())
            return self._credentials.token
        return self._token_value

    def _call(self, method: str, path: str, params: dict[str, str | list[str]]) -> dict:
        query = urllib.parse.urlencode(params, doseq=True)
        url = f"{_API}{path}?{query}" if query else f"{_API}{path}"
        token = self._bearer()
        headers = {"Authorization": f"Bearer {token}"}
        body = None
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                status, response_headers, raw = self._transport(url, headers, body)
            except OSError as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                self._sleep(self._backoff(attempt))
                continue
            if status == 429 or status >= 500:
                last_error = GmailRequestError(status, "retryable")
                if attempt == _MAX_ATTEMPTS:
                    raise last_error
                self._sleep(self._backoff(attempt))
                continue
            if status >= 400:
                raise GmailRequestError(status, _api_error(raw))
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise GmailRequestError(status, "invalid_json") from exc
            if not isinstance(parsed, dict):
                raise GmailRequestError(status, "invalid_json")
            return parsed
        raise last_error or GmailRequestError(0, "failed")

    def _backoff(self, attempt: int) -> float:
        return 0.5 * (2 ** (attempt - 1)) * (0.5 + self._random())


def gmail_client_from_env() -> GmailClient:
    mailbox = os.environ.get("GMAIL_MAILBOX", "").strip()
    oauth = _oauth_credentials()
    if oauth is not None:
        return GmailClient(mailbox, credentials=oauth)
    path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or "secrets/gdrive-service-account.json"
    return GmailClient(mailbox, credentials_file=path)


def _oauth_credentials():
    path = os.environ.get("GOOGLE_OAUTH_TOKEN_FILE", "").strip()
    if not path or not Path(path).is_file():
        return None
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_file(path)


def _load_credentials(path: str, mailbox: str):
    from google.oauth2 import service_account

    creds = service_account.Credentials.from_service_account_file(path, scopes=[_READONLY])
    return creds.with_subject(mailbox)


def _user(mailbox: str) -> str:
    return urllib.parse.quote(mailbox, safe="")


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
