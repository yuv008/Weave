"""Deterministic node ids.

Item and Source ids reproduce the formats already written by ``app/``:
- Slack message: ``{tenant}:slack:message:{channel}:{ts}``
- Slack channel: ``{tenant}:slack:conversation:{team}:{channel}``
- Notion or Drive document: ``{tenant}:{source}:document:{workspace}:{external_id}``

Entities are keyed by a normalized name, people by email, and claims by
their first evidence item plus the normalized statement.
"""

import hashlib
import re

_SLUG = re.compile(r"[^a-z0-9]+")
_SPACE = re.compile(r"\s+")


def slug(value: str) -> str:
    result = _SLUG.sub("-", value.strip().lower()).strip("-")
    if not result:
        raise ValueError("value has no characters to key on")
    return result


def normalize_statement(statement: str) -> str:
    return _SPACE.sub(" ", statement.strip().lower())


def item_id(tenant: str, source: str, object_type: str, *parts: str) -> str:
    return ":".join((tenant, source, object_type, *parts))


def slack_message_id(tenant: str, channel: str, ts: str) -> str:
    return item_id(tenant, "slack", "message", channel, ts)


def document_id(tenant: str, source: str, workspace: str, external_id: str) -> str:
    return item_id(tenant, source, "document", workspace, external_id)


def github_issue_id(tenant: str, repo: str, number: int) -> str:
    return item_id(tenant, "github", "issue", repo, str(number))


def gmail_message_id(tenant: str, mailbox: str, message_id: str) -> str:
    return item_id(tenant, "gmail", "message", mailbox, message_id)


def source_id(tenant: str, source: str, kind: str, *parts: str) -> str:
    return ":".join((tenant, source, kind, *parts))


def slack_channel_id(tenant: str, team: str, channel: str) -> str:
    return source_id(tenant, "slack", "conversation", team, channel)


def person_id(tenant: str, email: str) -> str:
    cleaned = email.strip().lower()
    if "@" not in cleaned:
        raise ValueError("person ids are keyed by email")
    return f"{tenant}:person:{cleaned}"


def entity_id(tenant: str, label: str, name: str) -> str:
    return f"{tenant}:{label.lower()}:{slug(name)}"


def claim_id(tenant: str, label: str, evidence_item_id: str, statement: str) -> str:
    key = f"{evidence_item_id}|{normalize_statement(statement)}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{tenant}:{label.lower()}:{digest}"


def projection_id(tenant: str, origin_id: str, audience: list[str]) -> str:
    key = f"{origin_id}|{'|'.join(sorted(audience))}"
    digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]
    return f"{tenant}:projection:{digest}"
