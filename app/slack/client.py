"""Slack Web API client for public channels.

The token is accepted only by the constructor and sent only in the
Authorization header. Callers read ``SLACK_BOT_TOKEN`` at the process edge.
"""

import json
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

_MAX_ATTEMPTS = 5
_API = "https://slack.com/api"


class SlackRequestError(Exception):
    """A Slack response that should not be retried, or retries that ran out."""

    def __init__(self, status: int, error: str):
        self.status = status
        self.error = error
        super().__init__(f"Slack request failed ({status}): {error}")


Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


class SlackClient:
    """Call conversations.list, conversations.history, and conversations.replies."""

    def __init__(
        self,
        token: str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
    ):
        if not isinstance(token, str) or not token:
            raise ValueError("Slack token is required")
        self._token = token
        self._transport = transport or _urlopen_transport
        self._sleep = sleep or time.sleep
        self._random = random_fn or random.random

    def list_public_channels(self, cursor: str | None = None) -> dict:
        params = {"types": "public_channel", "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        return self._call("conversations.list", params)

    def join_channel(self, channel_id: str) -> dict:
        """Join a public channel. ``already_in_channel`` counts as success."""

        try:
            return self._call("conversations.join", {"channel": channel_id}, post=True)
        except SlackRequestError as exc:
            if exc.error == "already_in_channel":
                return {"ok": True, "already_in_channel": True}
            raise

    def history(
        self,
        channel_id: str,
        cursor: str | None = None,
        oldest: str | None = None,
    ) -> dict:
        params = {"channel": channel_id, "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        if oldest:
            params["oldest"] = oldest
        return self._call("conversations.history", params)

    def replies(
        self,
        channel_id: str,
        parent_ts: str,
        cursor: str | None = None,
    ) -> dict:
        params = {"channel": channel_id, "ts": parent_ts, "limit": "200"}
        if cursor:
            params["cursor"] = cursor
        return self._call("conversations.replies", params)

    def fetch_message(self, channel_id: str, ts: str) -> dict:
        """Refetch one message without conversations.history.

        Slack returns the targeted message as the first row of
        conversations.replies. A reply timestamp that Slack cannot resolve
        stays on the failure list.
        """

        return self.replies(channel_id, ts)

    def user_profile(self, user_id: str) -> dict:
        """Return a display name and email. The token stays in the header."""

        payload = self._call("users.info", {"user": user_id})
        user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
        profile = user.get("profile") if isinstance(user.get("profile"), dict) else {}
        name = user.get("real_name") or profile.get("display_name") or ""
        email = profile.get("email") or ""
        return {
            "name": name if isinstance(name, str) else "",
            "email": email if isinstance(email, str) else "",
        }

    def _call(self, method: str, params: dict[str, str], *, post: bool = False) -> dict:
        query = urllib.parse.urlencode(params)
        request_body = query.encode() if post else None
        url = f"{_API}/{method}" if post else f"{_API}/{method}?{query}"
        if self._token in url or (request_body is not None and self._token.encode() in request_body):
            raise RuntimeError("refusing to place the Slack token in the request URL")
        headers = {"Authorization": f"Bearer {self._token}"}
        if post:
            headers["Content-Type"] = "application/x-www-form-urlencoded"
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                status, response_headers, body = self._transport(url, headers, request_body)
            except OSError as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                self._sleep(self._backoff(attempt))
                continue
            if status == 429:
                if attempt == _MAX_ATTEMPTS:
                    raise SlackRequestError(status, "ratelimited")
                self._sleep(_retry_after(response_headers))
                continue
            if status >= 500:
                last_error = SlackRequestError(status, "server_error")
                if attempt == _MAX_ATTEMPTS:
                    raise last_error
                self._sleep(self._backoff(attempt))
                continue
            if status >= 400:
                raise SlackRequestError(status, _slack_error(body))
            try:
                payload = json.loads(body)
            except json.JSONDecodeError as exc:
                raise SlackRequestError(status, "invalid_json") from exc
            if not isinstance(payload, dict) or not payload.get("ok"):
                error = payload.get("error") if isinstance(payload, dict) else "slack_error"
                if not isinstance(error, str) or not error:
                    error = "slack_error"
                raise SlackRequestError(status, error[:64])
            return payload
        raise last_error or SlackRequestError(0, "failed")

    def _backoff(self, attempt: int) -> float:
        return 0.5 * (2 ** (attempt - 1)) * (0.5 + self._random())


def _urlopen_transport(
    url: str,
    headers: dict[str, str],
    data: bytes | None = None,
) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=data, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, {key: value for key, value in response.headers.items()}, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, {key: value for key, value in exc.headers.items()}, exc.read()


def _retry_after(headers: dict[str, str]) -> float:
    lowered = {str(key).lower(): value for key, value in headers.items()}
    raw = lowered.get("retry-after", "1")
    try:
        return max(float(raw), 0)
    except (TypeError, ValueError):
        return 1.0


def _slack_error(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "http_error"
    error = payload.get("error") if isinstance(payload, dict) else None
    if isinstance(error, str) and error:
        return error[:64]
    return "http_error"
