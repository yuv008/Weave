"""Upsert one canonical Gmail message."""

import hashlib
from datetime import UTC, datetime

from app.graph.link_author import link_author
from app.graph.policy import allowed_source, gmail_source_id
from app.graph.upsert import UpsertResult
from app.models.canonical_gmail import CanonicalGmailMessage
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
  m.{Prop.SOURCE} = CASE WHEN do_apply THEN 'gmail' ELSE m.{Prop.SOURCE} END,
  m.connection_id = CASE WHEN do_apply THEN $connection_id ELSE m.connection_id END,
  m.workspace_id = CASE WHEN do_apply THEN $mailbox ELSE m.workspace_id END,
  m.{Prop.OBJECT_TYPE} = CASE WHEN do_apply THEN 'message' ELSE m.{Prop.OBJECT_TYPE} END,
  m.{Prop.EXTERNAL_ID} = CASE WHEN do_apply THEN $external_id ELSE m.{Prop.EXTERNAL_ID} END,
  m.{Prop.OCCURRED_AT} = CASE WHEN do_apply THEN $occurred_at ELSE m.{Prop.OCCURRED_AT} END,
  m.{Prop.VERSION} = CASE WHEN do_apply THEN $version ELSE m.{Prop.VERSION} END,
  m.{Prop.DELETED} = CASE WHEN do_apply THEN $deleted ELSE m.{Prop.DELETED} END,
  m.{Prop.EXCERPT} = CASE WHEN do_apply THEN $excerpt ELSE m.{Prop.EXCERPT} END,
  m.{Prop.CONTENT_HASH} = CASE WHEN do_apply THEN $content_hash ELSE m.{Prop.CONTENT_HASH} END,
  m.{Prop.SOURCE_URL} = CASE WHEN do_apply THEN $source_url ELSE m.{Prop.SOURCE_URL} END,
  m.visibility = CASE WHEN do_apply THEN $visibility ELSE m.visibility END,
  m.ingested_at = CASE WHEN do_apply THEN $now ELSE m.ingested_at END,
  m.placeholder = CASE WHEN do_apply THEN false ELSE coalesce(m.placeholder, false) END,
  m.{Prop.ACL_GROUPS} = CASE WHEN do_apply THEN $acl_groups ELSE m.{Prop.ACL_GROUPS} END,
  m.{Prop.MIN_LEVEL} = CASE WHEN do_apply THEN $min_level ELSE m.{Prop.MIN_LEVEL} END
RETURN do_apply
"""

_LINK_SOURCE = f"""
MERGE (s:{Label.SOURCE} {{id: $source_id}})
SET
  s.{Prop.TENANT_ID} = $tenant_id,
  s.{Prop.SOURCE} = 'gmail',
  s.connection_id = $connection_id,
  s.{Prop.EXTERNAL_ID} = $mailbox,
  s.{Prop.NAME} = $source_name,
  s.{Prop.ACL_GROUPS} = $acl_groups,
  s.{Prop.MIN_LEVEL} = $min_level,
  s.{Prop.CASCADE_POLICY} = $cascade_policy,
  s.{Prop.AUTHORITY} = $authority
MERGE (m:{Label.MESSAGE} {{id: $msg_id}})
MERGE (m)-[:{Rel.IN_SOURCE}]->(s)
"""

_LINK_PARENT = f"""
MERGE (parent:{Label.MESSAGE}:{Label.ITEM} {{id: $parent_id}})
ON CREATE SET
  parent.{Prop.TENANT_ID} = $tenant_id,
  parent.{Prop.SOURCE} = 'gmail',
  parent.connection_id = $connection_id,
  parent.workspace_id = $mailbox,
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
MERGE (parent)-[:{Rel.IN_SOURCE}]->(:{Label.SOURCE} {{id: $source_id}})
"""


def upsert_gmail_message(graph, message: CanonicalGmailMessage, *, now: datetime | None = None) -> UpsertResult:
    allowed = allowed_source("gmail", message.mailbox)
    if allowed is None:
        return UpsertResult(node_id=message.node_id, applied=False)
    group, policy = allowed
    ingested_at = now or datetime.now(UTC)
    if ingested_at.tzinfo is None:
        ingested_at = ingested_at.replace(tzinfo=UTC)
    params = _params(message, ingested_at, group, policy)
    rows = graph.query(_APPLY, params).result_set
    if not rows:
        return UpsertResult(node_id=message.node_id, applied=False)
    graph.query(_LINK_SOURCE, params)
    if message.author_email:
        link_author(
            graph,
            tenant_id=message.tenant_id,
            item_id=message.node_id,
            email=message.author_email,
            name=message.author_email,
        )
    if message.parent_node_id is not None and message.thread_parent_external_id:
        graph.query(
            _LINK_PARENT,
            {
                **params,
                "parent_id": message.parent_node_id,
                "parent_ext": message.thread_parent_external_id,
                "empty_hash": _EMPTY_HASH,
            },
        )
    return UpsertResult(node_id=message.node_id, applied=True)


def _params(message: CanonicalGmailMessage, ingested_at: datetime, group: str, policy) -> dict:
    excerpt = "" if message.deleted else message.excerpt
    return {
        "msg_id": message.node_id,
        "tenant_id": message.tenant_id,
        "connection_id": message.connection_id,
        "mailbox": message.mailbox,
        "external_id": message.external_id,
        "source_id": gmail_source_id(message.tenant_id, message.mailbox),
        "occurred_at": to_ts(message.occurred_at),
        "version": message.version,
        "deleted": message.deleted,
        "excerpt": excerpt,
        "content_hash": message.content_hash,
        "source_url": message.source_url,
        "visibility": message.visibility,
        "now": to_ts(ingested_at),
        "acl_groups": [group],
        "min_level": policy.min_level,
        "source_name": policy.name,
        "cascade_policy": policy.cascade_policy,
        "authority": policy.authority,
    }
