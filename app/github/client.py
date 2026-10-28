"""GitHub REST client for one repository.

The token is accepted only by the constructor and sent only in the
Authorization header. Callers read ``GITHUB_TOKEN`` at the process edge.
"""

import json
import os
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass

PINNED_GITHUB_API_VERSION = "2026-03-10"
_MAX_ATTEMPTS = 5
_API = "https://api.github.com"

Transport = Callable[[str, dict[str, str]], tuple[int, dict[str, str], bytes]]


class GitHubRequestError(Exception):
    def __init__(self, status: int, error: str):
        self.status = status
        self.error = error
        super().__init__(f"GitHub request failed ({status}): {error}")


@dataclass(frozen=True)
class GitHubResult:
    payload: dict | list
    next_page: str


class GitHubClient:
    def __init__(
        self,
        token: str,
        *,
        api_version: str | None = None,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
        now: Callable[[], float] | None = None,
    ):
        if not isinstance(token, str) or not token:
            raise ValueError("GitHub token is required")
        self._token = token
        self._api_version = api_version or PINNED_GITHUB_API_VERSION
        self._transport = transport or _urlopen_transport
        self._sleep = sleep or time.sleep
        self._random = random_fn or random.random
        self._now = now or time.time

    def get_repo(self, owner: str, repo: str) -> GitHubResult:
        return self._call(f"/repos/{_segment(owner)}/{_segment(repo)}", {})

    def list_installation_repositories(self) -> list[tuple[str, str]]:
        """Repositories this installation token can read. Owner and name only."""

        found: list[tuple[str, str]] = []
        page = ""
        while True:
            params = {"per_page": "100"}
            if page:
                params["page"] = page
            result = self._call("/installation/repositories", params)
            payload = result.payload if isinstance(result.payload, dict) else {}
            for repo in payload.get("repositories") or []:
                if not isinstance(repo, dict):
                    continue
                owner_obj = repo.get("owner") if isinstance(repo.get("owner"), dict) else {}
                owner = str(owner_obj.get("login") or "")
                name = str(repo.get("name") or "")
                if owner and name and "/" not in owner and "/" not in name:
                    found.append((owner, name))
            if not result.next_page:
                return found
            page = result.next_page

    def list_issues(self, owner: str, repo: str, page: str | None, since: str | None) -> GitHubResult:
        params = {
            "state": "all",
            "sort": "updated",
            "direction": "asc",
            "per_page": "100",
            "page": page or "1",
        }
        if since:
            params["since"] = since
        return self._call(f"/repos/{_segment(owner)}/{_segment(repo)}/issues", params)

    def get_issue(self, owner: str, repo: str, number: str) -> GitHubResult:
        return self._call(
            f"/repos/{_segment(owner)}/{_segment(repo)}/issues/{_segment(number)}",
            {},
        )

    def list_comments(
        self,
        owner: str,
        repo: str,
        number: str,
        page: str | None,
        since: str | None,
    ) -> GitHubResult:
        params = {"per_page": "100", "page": page or "1"}
        if since:
            params["since"] = since
        return self._call(
            f"/repos/{_segment(owner)}/{_segment(repo)}/issues/{_segment(number)}/comments",
            params,
        )

    def _call(self, path: str, params: dict[str, str]) -> GitHubResult:
        query = urllib.parse.urlencode(params)
        url = f"{_API}{path}" if not query else f"{_API}{path}?{query}"
        if self._token in url:
            raise RuntimeError("refusing to place the GitHub token in the request URL")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": self._api_version,
            "User-Agent": "weave",
        }
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
            if _is_rate_limit(status, response_headers, body):
                if attempt == _MAX_ATTEMPTS:
                    raise GitHubRequestError(status, "rate_limit")
                self._sleep(_retry_delay(response_headers, self._now))
                continue
            if status >= 500:
                last_error = GitHubRequestError(status, "server_error")
                if attempt == _MAX_ATTEMPTS:
                    raise last_error
                self._sleep(self._backoff(attempt))
                continue
            if status >= 400:
                raise GitHubRequestError(status, _github_error(body, self._token))
            try:
                payload = json.loads(body) if body else {}
            except json.JSONDecodeError as exc:
                raise GitHubRequestError(status, "invalid_json") from exc
            if not isinstance(payload, (dict, list)):
                raise GitHubRequestError(status, "invalid_json")
            return GitHubResult(payload=payload, next_page=_next_page(response_headers))
        raise last_error or GitHubRequestError(0, "failed")

    def _backoff(self, attempt: int) -> float:
        return 0.5 * (2 ** (attempt - 1)) * (0.5 + self._random())


def github_client_from_env() -> GitHubClient:
    from app.connect.credentials import github_access_token

    version = os.environ.get("GITHUB_API_VERSION", "").strip() or PINNED_GITHUB_API_VERSION
    return GitHubClient(github_access_token(), api_version=version)


def _segment(value: str) -> str:
    if not isinstance(value, str) or not value or "/" in value or ".." in value:
        raise ValueError("GitHub path segment is invalid")
    return urllib.parse.quote(value, safe="")


def _is_rate_limit(status: int, headers: dict[str, str], body: bytes) -> bool:
    if status == 429:
        return True
    if status != 403:
        return False
    lowered = {str(key).lower(): value for key, value in headers.items()}
    if lowered.get("x-ratelimit-remaining") == "0":
        return True
    message = _message(body).lower()
    return "rate limit" in message


def _retry_delay(headers: dict[str, str], now: Callable[[], float]) -> float:
    lowered = {str(key).lower(): value for key, value in headers.items()}
    raw = lowered.get("retry-after")
    if raw:
        try:
            return max(float(raw), 0)
        except ValueError:
            return 1.0
    reset = lowered.get("x-ratelimit-reset")
    if reset:
        try:
            return max(float(reset) - now(), 0)
        except ValueError:
            return 1.0
    return 1.0


def _next_page(headers: dict[str, str]) -> str:
    link = ""
    for key, value in headers.items():
        if str(key).lower() == "link":
            link = value
    if not link:
        return ""
    for part in link.split(","):
        if 'rel="next"' not in part:
            continue
        start = part.find("<")
        end = part.find(">")
        if start < 0 or end < 0:
            return ""
        query = urllib.parse.parse_qs(urllib.parse.urlparse(part[start + 1 : end]).query)
        page = (query.get("page") or [""])[0]
        return page if page.isdigit() else ""
    return ""


def _github_error(body: bytes, token: str) -> str:
    message = _message(body)
    if token and token in message:
        message = message.replace(token, "")
    return message[:64] or "http_error"


def _message(body: bytes) -> str:
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        return "http_error"
    if isinstance(payload, dict) and isinstance(payload.get("message"), str):
        return payload["message"]
    return "http_error"


def _urlopen_transport(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()
