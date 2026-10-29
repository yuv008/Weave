"""Idempotent FalkorDB upsert for one canonical Slack message.

Node ids:
- Message: ``{tenant}:slack:message:{channel}:{ts}``
- Conversation: ``{tenant}:slack:conversation:{team}:{channel}``
A message is ``:Message:Item``. A channel is ``:Conversation:Source``.
``IN_SOURCE`` is written on ingest; ``AUTHORED_BY`` is added after
``users.info`` resolves an email in ``app/slack/people.py``.
A channel missing from ``config/weave.yaml`` is not written.

Retries replay the same version. An equal version does not change stored
properties, and it does re-merge edges. An older version is a no-op.
"""

import hashlib
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.graph.link_author import purge_legacy_item_edges
from app.graph.policy import allowed_source, slack_source_id
from app.models.canonical import CanonicalMessage, message_node_id
from shared.config import source_acl
from shared.schema import Label, Prop, Rel
from shared.timefmt import to_ts

_EMPTY_HASH = hashlib.sha256(b"").hexdigest()

_APPLY = f"""
MERGE (m:{Label.MESSAGE}:{Label.ITEM} {{id: $msg_id}})
ON CREATE SET m.version = '', m.placeholder = false
WITH m,
  CASE
    WHEN coalesce(m.placeholder, false) = true
      OR coalesce(m.version, '') < $version THEN true
    ELSE false
  END AS do_apply
WHERE coalesce(m.placeholder, false) = true
   OR coalesce(m.version, '') < $version
   OR m.version = $version
SET
  m.{Prop.TENANT_ID} = CASE WHEN do_apply THEN $tenant_id ELSE m.{Prop.TENANT_ID} END,
  m.{Prop.SOURCE} = CASE WHEN do_apply THEN 'slack' ELSE m.{Prop.SOURCE} END,
  m.connection_id = CASE WHEN do_apply THEN $connection_id ELSE m.connection_id END,
  m.workspace_id = CASE WHEN do_apply THEN $workspace_id ELSE m.workspace_id END,
  m.{Prop.OBJECT_TYPE} = CASE WHEN do_apply THEN 'message' ELSE m.{Prop.OBJECT_TYPE} END,
  m.{Prop.EXTERNAL_ID} = CASE WHEN do_apply THEN $external_id ELSE m.{Prop.EXTERNAL_ID} END,
  m.{Prop.OCCURRED_AT} = CASE WHEN do_apply THEN $occurred_at ELSE m.{Prop.OCCURRED_AT} END,
  m.edited_at = CASE WHEN do_apply THEN $edited_at ELSE m.edited_at END,
  m.{Prop.VERSION} = CASE WHEN do_apply THEN $version ELSE m.{Prop.VERSION} END,
  m.{Prop.DELETED} = CASE WHEN do_apply THEN $deleted ELSE m.{Prop.DELETED} END,
  m.{Prop.EXCERPT} = CASE WHEN do_apply THEN $excerpt ELSE m.{Prop.EXCERPT} END,
  m.{Prop.CONTENT_HASH} = CASE WHEN do_apply THEN $content_hash ELSE m.{Prop.CONTENT_HASH} END,
  m.{Prop.SOURCE_URL} = CASE WHEN do_apply THEN $source_url ELSE m.{Prop.SOURCE_URL} END,
  m.visibility = CASE WHEN do_apply THEN $visibility ELSE m.visibility END,
  m.ingested_at = CASE WHEN do_apply THEN $now ELSE m.ingested_at END,
  m.placeholder = CASE WHEN do_apply THEN false ELSE coalesce(m.placeholder, false) END,
  m.{Prop.ACL_GROUPS} = CASE WHEN do_apply THEN $acl_groups ELSE m.{Prop.ACL_GROUPS} END,
  m.{Prop.MIN_LEVEL} = CASE WHEN do_apply THEN $min_level ELSE m.{Prop.MIN_LEVEL} END,
  m.author_external_id = CASE WHEN do_apply THEN $author_external_id ELSE m.author_external_id END
RETURN do_apply
"""

_LINK_CONVERSATION = f"""
MERGE (t:Tenant {{id: $tenant_id}})
MERGE (c:{Label.CONVERSATION}:{Label.SOURCE} {{id: $conv_id}})
SET
  c.{Prop.TENANT_ID} = $tenant_id,
  c.{Prop.SOURCE} = 'slack',
  c.connection_id = $connection_id,
  c.workspace_id = $workspace_id,
  c.{Prop.EXTERNAL_ID} = $conv_ext,
  c.{Prop.NAME} = $source_name,
  c.{Prop.ACL_GROUPS} = $acl_groups,
  c.{Prop.MIN_LEVEL} = $min_level,
  c.{Prop.CASCADE_POLICY} = $cascade_policy,
  c.{Prop.AUTHORITY} = $authority
MERGE (t)-[:HAS_CONVERSATION]->(c)
MERGE (m:{Label.MESSAGE} {{id: $msg_id}})
MERGE (m)-[:{Rel.IN_SOURCE}]->(c)
"""

_LINK_PARENT = f"""
MERGE (parent:{Label.MESSAGE}:{Label.ITEM} {{id: $parent_id}})
ON CREATE SET
  parent.{Prop.TENANT_ID} = $tenant_id,
  parent.{Prop.SOURCE} = 'slack',
  parent.connection_id = $connection_id,
  parent.workspace_id = $workspace_id,
  parent.{Prop.EXTERNAL_ID} = $parent_ext,
  parent.{Prop.OBJECT_TYPE} = 'message',
  parent.{Prop.OCCURRED_AT} = $occurred_at,
  parent.{Prop.SOURCE_URL} = '',
  parent.{Prop.EXCERPT} = '',
  parent.{Prop.CONTENT_HASH} = $empty_hash,
  parent.{Prop.DELETED} = false,
  parent.{Prop.ACL_GROUPS} = $acl_groups,
  parent.{Prop.MIN_LEVEL} = $min_level,
  parent.placeholder = true,
  parent.{Prop.VERSION} = ''
MERGE (m:{Label.MESSAGE} {{id: $msg_id}})
MERGE (m)-[:{Rel.REPLIES_TO}]->(parent)
MERGE (parent)-[:{Rel.IN_SOURCE}]->(:{Label.CONVERSATION} {{id: $conv_id}})
"""

_INDEXES = (
    "CREATE INDEX FOR (t:Tenant) ON (t.id)",
    "CREATE INDEX FOR (c:Conversation) ON (c.id)",
    "CREATE INDEX FOR (p:Person) ON (p.id)",
    f"CREATE INDEX FOR (m:{Label.MESSAGE}) ON (m.id)",
    f"CREATE INDEX FOR (m:{Label.MESSAGE}) ON (m.{Prop.TENANT_ID})",
    "CREATE INDEX FOR (c:Connection) ON (c.id)",
    f"CREATE INDEX FOR (d:{Label.DOCUMENT}) ON (d.id)",
    f"CREATE INDEX FOR (d:{Label.DOCUMENT}) ON (d.{Prop.TENANT_ID})",
    f"CREATE INDEX FOR (i:{Label.ISSUE}) ON (i.id)",
    f"CREATE INDEX FOR (d:{Label.DECISION}) ON (d.id)",
    "CREATE INDEX FOR (g:GithubDelivery) ON (g.id)",
)


class Graph(Protocol):
    def query(self, query: str, params: dict | None = None):
        """Run one parameterized Cypher query."""


@dataclass(frozen=True)
class UpsertResult:
    node_id: str
    applied: bool


def ensure_indexes(graph: Graph) -> None:
    for statement in _INDEXES:
        try:
            graph.query(statement)
        except Exception as exc:
            if "already" not in str(exc).lower():
                raise


def upsert_message(
    graph: Graph,
    message: CanonicalMessage,
    *,
    now: datetime | None = None,
) -> UpsertResult:
    """Upsert one message. An unknown channel is not written."""

    allowed = allowed_source("slack", message.conversation_external_id, message.tenant_id)
    if allowed is None:
        return UpsertResult(node_id=message.node_id, applied=False)
    group, policy = allowed
    ingested_at = now or datetime.now(UTC)
    if ingested_at.tzinfo is None:
        ingested_at = ingested_at.replace(tzinfo=UTC)
    params = _message_params(message, ingested_at, group, policy)
    result = graph.query(_APPLY, params)
    rows = result.result_set
    if not rows:
        return UpsertResult(node_id=message.node_id, applied=False)

    graph.query(_LINK_CONVERSATION, params)
    purge_legacy_item_edges(graph, message.node_id)
    if message.thread_parent_external_id is not None:
        graph.query(
            _LINK_PARENT,
            {
                **params,
                "parent_id": message_node_id(
                    message.tenant_id, message.thread_parent_external_id
                ),
                "parent_ext": message.thread_parent_external_id,
                "empty_hash": _EMPTY_HASH,
            },
        )
    return UpsertResult(node_id=message.node_id, applied=True)


def _message_params(message: CanonicalMessage, ingested_at: datetime, group: str, policy) -> dict:
    excerpt = "" if message.deleted else message.excerpt
    return {
        "msg_id": message.node_id,
        "tenant_id": message.tenant_id,
        "connection_id": message.connection_id,
        "workspace_id": message.workspace_id,
        "external_id": message.external_id,
        "conv_id": slack_source_id(
            message.tenant_id, message.workspace_id, message.conversation_external_id
        ),
        "conv_ext": message.conversation_external_id,
        "occurred_at": to_ts(message.occurred_at),
        "edited_at": None if message.edited_at is None else to_ts(message.edited_at),
        "version": message.version,
        "deleted": message.deleted,
        "excerpt": excerpt,
        "content_hash": message.content_hash,
        "source_url": message.source_url,
        "visibility": message.visibility,
        "now": to_ts(ingested_at),
        "acl_groups": source_acl(group, policy),
        "min_level": policy.min_level,
        "source_name": policy.name,
        "cascade_policy": policy.cascade_policy,
        "authority": policy.authority,
        "author_external_id": message.author_external_id or "",
    }
