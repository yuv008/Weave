"""Notion REST client.

The token is accepted only by the constructor. ``NOTION_VERSION`` defaults to
the pinned REST version ``2026-03-11`` when the environment value is empty.
"""

import json
import os
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable

PINNED_NOTION_VERSION = "2026-03-11"
_MAX_ATTEMPTS = 5
_API = "https://api.notion.com/v1"

Transport = Callable[[str, dict[str, str], bytes | None], tuple[int, dict[str, str], bytes]]


class NotionRequestError(Exception):
    def __init__(self, status: int, error: str):
        self.status = status
        self.error = error
        super().__init__(f"Notion request failed ({status}): {error}")


class NotionClient:
    def __init__(
        self,
        token: str,
        *,
        version: str = PINNED_NOTION_VERSION,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
    ):
        if not isinstance(token, str) or not token:
            raise ValueError("Notion token is required")
        if not version:
            raise ValueError("Notion-Version is required")
        self._token = token
        self._version = version
        self._transport = transport or _urlopen_transport
        self._sleep = sleep or time.sleep
        self._random = random_fn or random.random

    def users_me(self) -> dict:
        return self._call("GET", "/users/me")

    def search_pages(self, cursor: str | None = None, filter_last_edited_time_after: str | None = None) -> dict:
        """Search shared pages.

        Notion's search filter accepts an object type, not an edited-after
        timestamp. ``filter_last_edited_time_after`` is accepted so the worker
        can pass the watermark; the worker applies it after the response.
        """

        del filter_last_edited_time_after
        body: dict = {
            "filter": {"property": "object", "value": "page"},
            "sort": {"direction": "descending", "timestamp": "last_edited_time"},
            "page_size": 100,
        }
        if cursor:
            body["start_cursor"] = cursor
        return self._call("POST", "/search", body)

    def retrieve_page(self, page_id: str) -> dict:
        return self._call("GET", f"/pages/{page_id}")

    def block_children(self, block_id: str, cursor: str | None = None) -> dict:
        path = f"/blocks/{block_id}/children?page_size=100"
        if cursor:
            path += f"&start_cursor={cursor}"
        return self._call("GET", path)

    def _call(self, method: str, path: str, body: dict | None = None) -> dict:
        payload = None if body is None else json.dumps(body).encode()
        url = f"{_API}{path}"
        if self._token in url or (payload is not None and self._token.encode() in payload):
            raise RuntimeError("refusing to place the Notion token in the request")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Notion-Version": self._version,
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                status, response_headers, raw = self._transport(url, headers, payload if method == "POST" else None)
            except OSError as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                self._sleep(self._backoff(attempt))
                continue
            if status == 429:
                if attempt == _MAX_ATTEMPTS:
                    raise NotionRequestError(status, "rate_limited")
                self._sleep(_retry_after(response_headers))
                continue
            if status >= 500:
                last_error = NotionRequestError(status, "server_error")
                if attempt == _MAX_ATTEMPTS:
                    raise last_error
                self._sleep(self._backoff(attempt))
                continue
            if status >= 400:
                raise NotionRequestError(status, _error_code(raw))
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise NotionRequestError(status, "invalid_json") from exc
            if not isinstance(parsed, dict):
                raise NotionRequestError(status, "invalid_json")
            return parsed
        raise last_error or NotionRequestError(0, "failed")

    def _backoff(self, attempt: int) -> float:
        return 0.5 * (2 ** (attempt - 1)) * (0.5 + self._random())


def notion_client_from_env() -> NotionClient:
    version = os.environ.get("NOTION_VERSION", "").strip() or PINNED_NOTION_VERSION
    return NotionClient(os.environ.get("NOTION_TOKEN", ""), version=version)


def _urlopen_transport(url: str, headers: dict[str, str], data: bytes | None = None) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST" if data is not None else "GET")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _retry_after(headers: dict[str, str]) -> float:
    lowered = {str(key).lower(): value for key, value in headers.items()}
    try:
        return max(float(lowered.get("retry-after", "1")), 0)
    except (TypeError, ValueError):
        return 1.0


def _error_code(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "http_error"
    code = payload.get("code") if isinstance(payload, dict) else None
    if isinstance(code, str) and code:
        return code[:64]
    return "http_error"
