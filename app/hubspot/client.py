"""HubSpot CRM search client."""

import json
import os
import random
import time
import urllib.error
import urllib.request
from collections.abc import Callable

_MAX_ATTEMPTS = 5
_API = "https://api.hubapi.com"

Transport = Callable[[str, dict[str, str], bytes], tuple[int, dict[str, str], bytes]]


class HubspotRequestError(Exception):
    def __init__(self, status: int, error: str):
        self.status = status
        self.error = error
        super().__init__(f"HubSpot request failed ({status}): {error}")


class HubspotClient:
    def __init__(
        self,
        token: str,
        *,
        transport: Transport | None = None,
        sleep: Callable[[float], None] | None = None,
        random_fn: Callable[[], float] | None = None,
    ):
        if not token:
            raise ValueError("HubSpot token is required")
        self._token = token
        self._transport = transport or _urlopen_transport
        self._sleep = sleep or time.sleep
        self._random = random_fn or random.random

    def search_companies(self, modified_after_ms: int, after: str | None = None) -> dict:
        return self._search(
            "companies",
            modified_after_ms,
            after,
            ["name", "hubspot_owner_id", "hs_lastmodifieddate"],
        )

    def search_deals(self, modified_after_ms: int, after: str | None = None) -> dict:
        return self._search(
            "deals",
            modified_after_ms,
            after,
            ["dealname", "dealstage", "closedate", "hubspot_owner_id", "hs_lastmodifieddate", "associatedcompanyid"],
        )

    def list_owner_emails(self) -> dict[str, str]:
        """Map HubSpot owner id to email. Requires ``crm.objects.owners.read``."""
        out: dict[str, str] = {}
        after: str | None = None
        while True:
            path = "/crm/v3/owners?limit=100"
            if after:
                path += f"&after={after}"
            page = self._call_get(path)
            results = page.get("results") if isinstance(page.get("results"), list) else []
            for row in results:
                if not isinstance(row, dict):
                    continue
                oid = row.get("id")
                email = row.get("email")
                if oid is not None and isinstance(email, str) and "@" in email:
                    out[str(oid)] = email.strip().lower()
            paging = page.get("paging") if isinstance(page.get("paging"), dict) else {}
            next_after = paging.get("next", {}).get("after") if isinstance(paging.get("next"), dict) else None
            if not next_after:
                break
            after = str(next_after)
        return out

    def _search(self, object_type: str, modified_after_ms: int, after: str | None, properties: list[str]) -> dict:
        body = {
            "filterGroups": [
                {
                    "filters": [
                        {
                            "propertyName": "hs_lastmodifieddate",
                            "operator": "GT",
                            "value": str(modified_after_ms),
                        }
                    ]
                }
            ],
            "properties": properties,
            "limit": 100,
        }
        if after:
            body["after"] = after
        return self._call("POST", f"/crm/v3/objects/{object_type}/search", body)

    def _call(self, method: str, path: str, body: dict) -> dict:
        url = f"{_API}{path}"
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        }
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                status, _hdrs, raw = self._transport(url, headers, payload)
            except OSError as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                self._sleep(self._backoff(attempt))
                continue
            if status == 429 or status >= 500:
                last_error = HubspotRequestError(status, "retryable")
                if attempt == _MAX_ATTEMPTS:
                    raise last_error
                self._sleep(self._backoff(attempt))
                continue
            if status >= 400:
                raise HubspotRequestError(status, _api_error(raw))
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise HubspotRequestError(status, "invalid_json") from exc
            if not isinstance(parsed, dict):
                raise HubspotRequestError(status, "invalid_json")
            return parsed
        raise last_error or HubspotRequestError(0, "failed")

    def _call_get(self, path: str) -> dict:
        url = f"{_API}{path}"
        headers = {"Authorization": f"Bearer {self._token}"}
        last_error: Exception | None = None
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            try:
                status, _hdrs, raw = _urlopen_get(url, headers)
            except OSError as exc:
                last_error = exc
                if attempt == _MAX_ATTEMPTS:
                    raise
                self._sleep(self._backoff(attempt))
                continue
            if status == 429 or status >= 500:
                last_error = HubspotRequestError(status, "retryable")
                if attempt == _MAX_ATTEMPTS:
                    raise last_error
                self._sleep(self._backoff(attempt))
                continue
            if status >= 400:
                raise HubspotRequestError(status, _api_error(raw))
            try:
                parsed = json.loads(raw) if raw else {}
            except json.JSONDecodeError as exc:
                raise HubspotRequestError(status, "invalid_json") from exc
            if not isinstance(parsed, dict):
                raise HubspotRequestError(status, "invalid_json")
            return parsed
        raise last_error or HubspotRequestError(0, "failed")

    def _backoff(self, attempt: int) -> float:
        return 0.5 * (2 ** (attempt - 1)) * (0.5 + self._random())


def hubspot_token_from_env() -> str:
    return os.environ.get("HUBSPOT_TOKEN", "").strip() or os.environ.get("HUBSPOT_ACCESS_TOKEN", "").strip()


def hubspot_client_from_env() -> HubspotClient:
    return HubspotClient(hubspot_token_from_env())


def _urlopen_transport(url: str, headers: dict[str, str], body: bytes) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, data=body, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, dict(response.headers.items()), response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, dict(exc.headers.items()), exc.read()


def _urlopen_get(url: str, headers: dict[str, str]) -> tuple[int, dict[str, str], bytes]:
    request = urllib.request.Request(url, headers=headers, method="GET")
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
    message = payload.get("message") if isinstance(payload, dict) else None
    if isinstance(message, str):
        return message[:96]
    return "http_error"
