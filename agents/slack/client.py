"""Slack Web API calls for answering mentions and DMs.

The token is sent only in the Authorization header. This is separate from
``app.slack.client``, which reads channels and does not post.
"""

import json
import urllib.error
import urllib.parse
import urllib.request

_API = "https://slack.com/api"


class SlackError(Exception):
    def __init__(self, error: str):
        self.error = error
        super().__init__(error)


class SlackApi:
    def __init__(self, token: str, *, http=None):
        if not isinstance(token, str) or not token:
            raise ValueError("Slack token is required")
        self._token = token
        self._http = http or _urlopen
        self._bot: str | None = None

    def __repr__(self) -> str:
        return "SlackApi(token=***)"

    def bot_user(self) -> str:
        if self._bot is None:
            self._bot = str(self._call("auth.test", {}, post=True).get("user_id") or "")
        return self._bot

    def members(self, channel_id: str) -> list[str]:
        found: list[str] = []
        cursor = ""
        while True:
            params = {"channel": channel_id, "limit": "200"}
            if cursor:
                params["cursor"] = cursor
            payload = self._call("conversations.members", params)
            found.extend(m for m in payload.get("members") or [] if isinstance(m, str))
            cursor = str((payload.get("response_metadata") or {}).get("next_cursor") or "")
            if not cursor:
                return found

    def post_message(self, channel_id: str, text: str, thread_ts: str) -> dict:
        return self._call(
            "chat.postMessage",
            {"channel": channel_id, "text": text, "thread_ts": thread_ts},
            post=True,
        )

    def _call(self, method: str, params: dict, *, post: bool = False) -> dict:
        payload = self._http(method, dict(params), post, self._token)
        if not isinstance(payload, dict) or not payload.get("ok"):
            error = payload.get("error") if isinstance(payload, dict) else None
            if not isinstance(error, str) or not error:
                error = "slack_error"
            raise SlackError(error[:64])
        return payload


def _urlopen(method: str, params: dict, post: bool, token: str) -> dict:
    query = urllib.parse.urlencode(params)
    body = query.encode() if post else None
    url = f"{_API}/{method}" if post else f"{_API}/{method}?{query}"
    if token in url or (body is not None and token.encode() in body):
        raise RuntimeError("refusing to place the Slack token in the request URL")
    headers = {"Authorization": f"Bearer {token}"}
    if post:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    request = urllib.request.Request(url, data=body, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        raw = exc.read()
    except OSError as exc:
        raise SlackError("network_error") from exc
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SlackError("invalid_json") from exc
    return payload if isinstance(payload, dict) else {"ok": False, "error": "slack_error"}
