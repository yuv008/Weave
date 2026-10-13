"""Idempotent FalkorDB upsert for one canonical Slack message.

Node ids:
- Message: ``{tenant}:slack:message:{channel}:{ts}``
- Conversation: ``{tenant}:slack:conversation:{team}:{channel}``
- Person: ``{tenant}:slack:person:{team}:{user}``

Relationship direction:
- ``(Tenant)-[:HAS_CONVERSATION]->(Conversation)``
- ``(Conversation)-[:CONTAINS]->(Message)``
- ``(Person)-[:AUTHORED]->(Message)``
- ``(Message)-[:REPLIES_TO]->(parent Message)``

A reply may arrive before its parent. The parent is then a placeholder
with ``version == ''`` and ``placeholder == true``. The real parent write
matches that node and fills it in.

Retries replay the same version. An equal version does not change stored
properties, and it does re-merge edges, so a crash between the property
write and the edge write can be retried. An older version is a no-op,
including its edges.
"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

from app.models.canonical import CanonicalMessage, message_node_id

_APPLY = """
MERGE (m:Message {id: $msg_id})
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
  m.tenant_id = CASE WHEN do_apply THEN $tenant_id ELSE m.tenant_id END,
  m.source = CASE WHEN do_apply THEN 'slack' ELSE m.source END,
  m.connection_id = CASE WHEN do_apply THEN $connection_id ELSE m.connection_id END,
  m.workspace_id = CASE WHEN do_apply THEN $workspace_id ELSE m.workspace_id END,
  m.object_type = CASE WHEN do_apply THEN 'message' ELSE m.object_type END,
  m.external_id = CASE WHEN do_apply THEN $external_id ELSE m.external_id END,
  m.occurred_at = CASE WHEN do_apply THEN $occurred_at ELSE m.occurred_at END,
  m.edited_at = CASE WHEN do_apply THEN $edited_at ELSE m.edited_at END,
  m.version = CASE WHEN do_apply THEN $version ELSE m.version END,
  m.deleted = CASE WHEN do_apply THEN $deleted ELSE m.deleted END,
  m.excerpt = CASE WHEN do_apply THEN $excerpt ELSE m.excerpt END,
  m.content_hash = CASE WHEN do_apply THEN $content_hash ELSE m.content_hash END,
  m.source_url = CASE WHEN do_apply THEN $source_url ELSE m.source_url END,
  m.visibility = CASE WHEN do_apply THEN $visibility ELSE m.visibility END,
  m.ingested_at = CASE WHEN do_apply THEN $now ELSE m.ingested_at END,
  m.placeholder = CASE WHEN do_apply THEN false ELSE coalesce(m.placeholder, false) END
RETURN do_apply
"""

_LINK_CONVERSATION = """
MERGE (t:Tenant {id: $tenant_id})
MERGE (c:Conversation {id: $conv_id})
ON CREATE SET
  c.tenant_id = $tenant_id,
  c.source = 'slack',
  c.connection_id = $connection_id,
  c.workspace_id = $workspace_id,
  c.external_id = $conv_ext
MERGE (t)-[:HAS_CONVERSATION]->(c)
MERGE (m:Message {id: $msg_id})
MERGE (c)-[:CONTAINS]->(m)
"""

_LINK_AUTHOR = """
MERGE (p:Person {id: $person_id})
ON CREATE SET
  p.tenant_id = $tenant_id,
  p.source = 'slack',
  p.connection_id = $connection_id,
  p.workspace_id = $workspace_id,
  p.external_id = $author_ext
MERGE (m:Message {id: $msg_id})
MERGE (p)-[:AUTHORED]->(m)
"""

_LINK_PARENT = """
MERGE (parent:Message {id: $parent_id})
ON CREATE SET
  parent.tenant_id = $tenant_id,
  parent.source = 'slack',
  parent.connection_id = $connection_id,
  parent.workspace_id = $workspace_id,
  parent.external_id = $parent_ext,
  parent.placeholder = true,
  parent.version = ''
MERGE (m:Message {id: $msg_id})
MERGE (m)-[:REPLIES_TO]->(parent)
"""

_INDEXES = (
    "CREATE INDEX FOR (t:Tenant) ON (t.id)",
    "CREATE INDEX FOR (c:Conversation) ON (c.id)",
    "CREATE INDEX FOR (p:Person) ON (p.id)",
    "CREATE INDEX FOR (m:Message) ON (m.id)",
    "CREATE INDEX FOR (m:Message) ON (m.tenant_id)",
    "CREATE INDEX FOR (s:SyncState) ON (s.id)",
    "CREATE INDEX FOR (f:SyncFailure) ON (f.id)",
    "CREATE INDEX FOR (e:SlackEvent) ON (e.id)",
    "CREATE INDEX FOR (c:Connection) ON (c.id)",
    "CREATE INDEX FOR (d:Document) ON (d.id)",
    "CREATE INDEX FOR (d:Document) ON (d.tenant_id)",
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
    """Upsert one message. Text leaves this function only as a parameter."""

    ingested_at = now or datetime.now(UTC)
    if ingested_at.tzinfo is None:
        ingested_at = ingested_at.replace(tzinfo=UTC)
    params = _message_params(message, ingested_at)
    result = graph.query(_APPLY, params)
    rows = result.result_set
    if not rows:
        return UpsertResult(node_id=message.node_id, applied=False)

    graph.query(_LINK_CONVERSATION, params)
    if message.person_node_id is not None:
        graph.query(
            _LINK_AUTHOR,
            {
                **params,
                "person_id": message.person_node_id,
                "author_ext": message.author_external_id,
            },
        )
    if message.thread_parent_external_id is not None:
        graph.query(
            _LINK_PARENT,
            {
                **params,
                "parent_id": message_node_id(
                    message.tenant_id, message.thread_parent_external_id
                ),
                "parent_ext": message.thread_parent_external_id,
            },
        )
    return UpsertResult(node_id=message.node_id, applied=True)


def _message_params(message: CanonicalMessage, ingested_at: datetime) -> dict:
    excerpt = "" if message.deleted else message.excerpt
    return {
        "msg_id": message.node_id,
        "tenant_id": message.tenant_id,
        "connection_id": message.connection_id,
        "workspace_id": message.workspace_id,
        "external_id": message.external_id,
        "conv_id": message.conversation_node_id,
        "conv_ext": message.conversation_external_id,
        "occurred_at": _iso(message.occurred_at),
        "edited_at": _iso(message.edited_at),
        "version": message.version,
        "deleted": message.deleted,
        "excerpt": excerpt,
        "content_hash": message.content_hash,
        "source_url": message.source_url,
        "visibility": message.visibility,
        "now": _iso(ingested_at),
    }


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
