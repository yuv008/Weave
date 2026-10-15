"""Slack sender: delivers ready ``slack_dm`` tasks after re-gating them for the recipient.

Two producers queue DMs: ``confirm_action`` (an accepted note) and the impact
watcher (a notice). At send time the recipient's own principal must still see
at least one evidence id, or the task expires unsent. Delivery is a dry run
unless the caller picks Slack for a real tenant with a bot token; the token
is held only by ``SlackTransport`` and sent only in the Authorization header.
"""

import json
import logging
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Protocol

from agents.gate.principal import Surface
from agents.gate.resolve import NotAuthorized
from agents.memory.store import Task
from agents.runtime import Runtime
from agents.tools.common import visible_node_ids
from shared.schema import Label, Prop

log = logging.getLogger("weave.watcher")

SLACK_SENDER = "slack_sender"
SLACK_DM = "slack_dm"
SEND_MODES = ("dry_run", "slack")
DRAFT_POINTER = "Draft customer update ready to review: {proposal_id}"

_API = "https://slack.com/api"
_SLACK_USER = re.compile(r"[UW][A-Z0-9]{8,}")
_SAFE_ERROR = re.compile(r"[^a-z0-9_]")

_SLACK_ID = f"MATCH (p:{Label.PERSON} {{{Prop.ID}: $id}}) RETURN p.{Prop.SLACK_ID}"

Http = Callable[[str, dict[str, str], bytes], tuple[int, bytes]]


class SlackSendError(Exception):
    """A failed Slack call. ``error`` is Slack's short error code, never request data."""

    def __init__(self, error: str):
        self.error = _SAFE_ERROR.sub("", str(error).lower())[:64] or "slack_error"
        super().__init__(f"Slack send failed: {self.error}")


class Transport(Protocol):
    requires_slack_id: bool

    def send(self, person_id: str, slack_id: str | None, text: str) -> dict: ...


class DryRunTransport:
    """Records what would be sent. No network."""

    requires_slack_id = False

    def send(self, person_id: str, slack_id: str | None, text: str) -> dict:
        return {"transport": "dry_run", "to": person_id}


class SlackTransport:
    """``conversations.open`` then ``chat.postMessage`` with a bot token."""

    requires_slack_id = True

    def __init__(self, token: str, *, http: Http | None = None):
        if not isinstance(token, str) or not token:
            raise ValueError("Slack token is required")
        self._token = token
        self._http = http or _urlopen

    def __repr__(self) -> str:
        return "SlackTransport()"

    def send(self, person_id: str, slack_id: str | None, text: str) -> dict:
        if not slack_id:
            raise SlackSendError("no_slack_id")
        opened = self._call("conversations.open", {"users": slack_id})
        channel = opened.get("channel")
        channel_id = channel.get("id") if isinstance(channel, dict) else None
        if not isinstance(channel_id, str) or not channel_id:
            raise SlackSendError("no_channel")
        posted = self._call("chat.postMessage", {"channel": channel_id, "text": text})
        return {"transport": "slack", "to": person_id, "channel": channel_id, "ts": posted.get("ts")}

    def _call(self, method: str, params: dict[str, str]) -> dict:
        headers = {
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/x-www-form-urlencoded; charset=utf-8",
        }
        try:
            status, body = self._http(f"{_API}/{method}", headers, urllib.parse.urlencode(params).encode())
        except OSError:
            raise SlackSendError("network_error") from None
        if status == 429:
            raise SlackSendError("ratelimited")
        try:
            payload = json.loads(body)
        except (TypeError, ValueError):
            raise SlackSendError(f"http_{status}") from None
        if status >= 400 or not isinstance(payload, dict) or not payload.get("ok"):
            error = payload.get("error") if isinstance(payload, dict) else None
            raise SlackSendError(error if isinstance(error, str) and error else f"http_{status}")
        return payload


def _urlopen(url: str, headers: dict[str, str], data: bytes) -> tuple[int, bytes]:
    request = urllib.request.Request(url, data=data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def transport_for(mode: str, tenant: str, token: str | None, *, fixture_tenant: str) -> Transport:
    """Slack only for ``mode == "slack"``, a non-fixture tenant and a token; otherwise a dry run."""

    if mode not in SEND_MODES:
        raise ValueError(f"send mode must be one of {list(SEND_MODES)}")
    if mode == "slack" and tenant != fixture_tenant and token:
        return SlackTransport(token)
    return DryRunTransport()


def slack_id_for(runtime: Runtime, tenant: str, person_id: str) -> str | None:
    """The person's Slack user id from the graph, if it looks like a real one."""

    rows = runtime.open_graph(tenant).ro_query(_SLACK_ID, {"id": person_id}).result_set
    value = rows[0][0] if rows else None
    return value if isinstance(value, str) and _SLACK_USER.fullmatch(value) else None


def draft_pointer(memory, task: Task) -> str | None:
    """The review pointer, once the linked handoff is done for the same recipient."""

    handoff_id = task.payload.get("handoff_id")
    if not isinstance(handoff_id, str) or not handoff_id:
        return None
    handoff = memory.get_task(handoff_id)
    if (handoff is None or handoff.tenant != task.tenant or handoff.state != "done"
            or handoff.payload.get("recipient") != task.payload.get("recipient")):
        return None
    proposal_id = handoff.payload.get("proposal_id")
    return DRAFT_POINTER.format(proposal_id=proposal_id) if proposal_id else None


def _finish(runtime: Runtime, task: Task, state: str, **payload) -> dict:
    runtime.memory.update_task(task.id, state=state, payload=payload)
    return {"task_id": task.id, "recipient": task.payload.get("recipient"), "state": state, **payload}


def send_one(runtime: Runtime, task: Task, transport: Transport) -> dict:
    """Re-gate one claimed ``slack_dm`` task and deliver it. Returns the outcome."""

    recipient = task.payload.get("recipient")
    text = task.payload.get("text")
    if not isinstance(recipient, str) or not recipient:
        return _finish(runtime, task, "failed", reason="no recipient")
    if not isinstance(text, str) or not text.strip():
        return _finish(runtime, task, "failed", reason="no text")
    try:
        principal = runtime.resolver.for_person(task.tenant, recipient, Surface.SLACK_DM)
    except NotAuthorized:
        return _finish(runtime, task, "expired", reason="recipient unknown")
    evidence = list(task.payload.get("evidence_ids") or [])
    if not visible_node_ids(runtime.context(principal, agent=SLACK_SENDER), evidence):
        return _finish(runtime, task, "expired", reason="evidence no longer visible")

    pointer = draft_pointer(runtime.memory, task)
    if pointer:
        text = f"{text}\n\n{pointer}"
    slack_id = None
    if transport.requires_slack_id:
        slack_id = slack_id_for(runtime, task.tenant, recipient)
        if slack_id is None:
            return _finish(runtime, task, "failed", reason="no slack id")
    try:
        receipt = transport.send(recipient, slack_id, text)
    except SlackSendError as exc:
        return _finish(runtime, task, "failed", reason=f"slack: {exc.error}")
    except Exception as exc:
        return _finish(runtime, task, "failed", reason=f"transport: {type(exc).__name__}")
    delivery = {**receipt, "text": text, "at": runtime.clock().isoformat()}
    return _finish(runtime, task, "sent", delivery=delivery)


def run_sends(runtime: Runtime, tenant: str, worker: str, transport: Transport | None = None,
              limit: int = 50) -> list[dict]:
    """Claim and deliver up to ``limit`` ready DMs (SKIP LOCKED in Postgres). Dry run by default."""

    transport = transport or DryRunTransport()
    outcomes = []
    while len(outcomes) < limit:
        task = runtime.memory.claim_task(tenant, SLACK_DM, SLACK_SENDER, worker)
        if task is None:
            break
        try:
            outcomes.append(send_one(runtime, task, transport))
        except Exception as exc:
            log.warning("slack_dm %s failed: %s", task.id, type(exc).__name__)
            outcomes.append(_finish(runtime, task, "failed", reason=f"error: {type(exc).__name__}"))
    return outcomes
