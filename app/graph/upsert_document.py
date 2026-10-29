"""Idempotent FalkorDB upsert for one canonical document.

Notion and Drive pages are ``:Document:Item``. GitHub issues and pull
requests are ``:Issue:Item``. Comments stay ``:Document:Item``.
A source missing from ``config/weave.yaml`` is not written.

An equal version keeps the stored excerpt and re-merges edges. An older
version does not write. A comment may arrive before its issue.
"""

import hashlib
from datetime import UTC, datetime

from app.graph.policy import (
    SOURCE_LABELS,
    allowed_source,
    drive_source_id,
    gcal_source_id,
    github_source_id,
    hubspot_source_id,
    notion_source_id,
)
from app.graph.link_author import link_author, purge_legacy_item_edges
from app.graph.upsert import UpsertResult
from app.models.canonical_document import CanonicalDocument
from shared.config import source_acl
from shared.schema import Label, Prop, Rel
from shared.timefmt import to_ts

_EMPTY_HASH = hashlib.sha256(b"").hexdigest()


def _apply(label: str) -> str:
    return f"""
MERGE (d:{label}:{Label.ITEM} {{id: $doc_id}})
ON CREATE SET d.version = '', d.placeholder = false
WITH d,
  CASE
    WHEN coalesce(d.placeholder, false) = true
      OR coalesce(d.version, '') < $version THEN true
    ELSE false
  END AS do_apply
WHERE coalesce(d.placeholder, false) = true
   OR coalesce(d.version, '') < $version
   OR d.version = $version
SET
  d.{Prop.TENANT_ID} = CASE WHEN do_apply THEN $tenant_id ELSE d.{Prop.TENANT_ID} END,
  d.{Prop.SOURCE} = CASE WHEN do_apply THEN $source ELSE d.{Prop.SOURCE} END,
  d.connection_id = CASE WHEN do_apply THEN $connection_id ELSE d.connection_id END,
  d.workspace_id = CASE WHEN do_apply THEN $workspace_id ELSE d.workspace_id END,
  d.{Prop.EXTERNAL_ID} = CASE WHEN do_apply THEN $external_id ELSE d.{Prop.EXTERNAL_ID} END,
  d.{Prop.OBJECT_TYPE} = CASE WHEN do_apply THEN $object_type ELSE d.{Prop.OBJECT_TYPE} END,
  d.{Prop.TITLE} = CASE WHEN do_apply THEN $title ELSE d.{Prop.TITLE} END,
  d.{Prop.OCCURRED_AT} = CASE WHEN do_apply THEN $occurred_at ELSE d.{Prop.OCCURRED_AT} END,
  d.updated_at = CASE WHEN do_apply THEN $updated_at ELSE d.updated_at END,
  d.{Prop.VERSION} = CASE WHEN do_apply THEN $version ELSE d.{Prop.VERSION} END,
  d.{Prop.DELETED} = CASE WHEN do_apply THEN $deleted ELSE d.{Prop.DELETED} END,
  d.{Prop.EXCERPT} = CASE WHEN do_apply THEN $excerpt ELSE d.{Prop.EXCERPT} END,
  d.{Prop.CONTENT_HASH} = CASE WHEN do_apply THEN $content_hash ELSE d.{Prop.CONTENT_HASH} END,
  d.{Prop.SOURCE_URL} = CASE WHEN do_apply THEN $source_url ELSE d.{Prop.SOURCE_URL} END,
  d.visibility = CASE WHEN do_apply THEN $visibility ELSE d.visibility END,
  d.ingested_at = CASE WHEN do_apply THEN $now ELSE d.ingested_at END,
  d.kind = CASE WHEN do_apply THEN $kind ELSE d.kind END,
  d.status = CASE WHEN do_apply THEN $status ELSE d.status END,
  d.author_external_id = CASE WHEN do_apply THEN $author_external_id ELSE d.author_external_id END,
  d.author_email = CASE WHEN do_apply THEN $author_email ELSE d.author_email END,
  d.parent_external_id = CASE WHEN do_apply THEN $parent_external_id ELSE d.parent_external_id END,
  d.placeholder = CASE WHEN do_apply THEN false ELSE coalesce(d.placeholder, false) END,
  d.{Prop.ACL_GROUPS} = CASE WHEN do_apply THEN $acl_groups ELSE d.{Prop.ACL_GROUPS} END,
  d.{Prop.MIN_LEVEL} = CASE WHEN do_apply THEN $min_level ELSE d.{Prop.MIN_LEVEL} END
RETURN do_apply
"""


def _link(source_labels: str, item_label: str) -> str:
    return f"""
MERGE (c:Connection {{id: $connection_node_id}})
ON CREATE SET
  c.tenant_id = $tenant_id,
  c.connection_id = $connection_id,
  c.source = $source,
  c.workspace_id = $workspace_id
MERGE (s:{source_labels} {{id: $source_id}})
SET
  s.{Prop.TENANT_ID} = $tenant_id,
  s.{Prop.SOURCE} = $source,
  s.{Prop.EXTERNAL_ID} = $source_external_id,
  s.{Prop.NAME} = $source_name,
  s.{Prop.ACL_GROUPS} = $acl_groups,
  s.{Prop.MIN_LEVEL} = $min_level,
  s.{Prop.CASCADE_POLICY} = $cascade_policy,
  s.{Prop.AUTHORITY} = $authority
MERGE (d:{item_label} {{id: $doc_id}})
MERGE (c)-[:HAS_SOURCE]->(d)
MERGE (d)-[:{Rel.IN_SOURCE}]->(s)
"""


def _link_parent(parent_label: str, item_label: str) -> str:
    return f"""
MERGE (parent:{parent_label}:{Label.ITEM} {{id: $parent_id}})
ON CREATE SET
  parent.{Prop.TENANT_ID} = $tenant_id,
  parent.{Prop.SOURCE} = $source,
  parent.connection_id = $connection_id,
  parent.workspace_id = $workspace_id,
  parent.{Prop.EXTERNAL_ID} = $parent_ext,
  parent.{Prop.OBJECT_TYPE} = $parent_object_type,
  parent.{Prop.OCCURRED_AT} = $occurred_at,
  parent.{Prop.SOURCE_URL} = '',
  parent.{Prop.EXCERPT} = '',
  parent.{Prop.CONTENT_HASH} = $empty_hash,
  parent.{Prop.DELETED} = false,
  parent.{Prop.ACL_GROUPS} = $acl_groups,
  parent.{Prop.MIN_LEVEL} = $min_level,
  parent.placeholder = true,
  parent.{Prop.VERSION} = ''
MERGE (d:{item_label} {{id: $doc_id}})
MERGE (d)-[:{Rel.REPLIES_TO}]->(parent)
MERGE (parent)-[:{Rel.IN_SOURCE}]->(:{Label.SOURCE} {{id: $source_id}})
"""


def upsert_document(graph, document: CanonicalDocument, *, now: datetime | None = None, store=None):
    """Upsert one document. An unknown source is not written. With a store, a new version is re-chunked."""

    allowed = _allowed(document)
    if allowed is None:
        return UpsertResult(node_id=document.node_id, applied=False)
    system, group, policy, source_node = allowed
    ingested_at = now or datetime.now(UTC)
    if ingested_at.tzinfo is None:
        ingested_at = ingested_at.replace(tzinfo=UTC)
    item_label = _item_label(document)
    params = _params(document, ingested_at, system, group, policy, source_node)
    rows = graph.query(_apply(item_label), params).result_set
    if not rows:
        return UpsertResult(node_id=document.node_id, applied=False)
    labels = ":".join(SOURCE_LABELS[system])
    graph.query(_link(labels, item_label), params)
    purge_legacy_item_edges(graph, document.node_id)
    if document.parent_node_id:
        parent_label = Label.ISSUE if document.source == "github" else Label.DOCUMENT
        graph.query(
            _link_parent(parent_label, item_label),
            {
                **params,
                "parent_id": document.parent_node_id,
                "parent_ext": document.parent_external_id,
                "parent_object_type": "issue" if parent_label == Label.ISSUE else "document",
                "empty_hash": _EMPTY_HASH,
            },
        )
        purge_legacy_item_edges(graph, document.node_id)
    if store is not None and rows[0][0]:
        from app.ingest.chunks import write_item_chunks  # app.ingest imports this module

        write_item_chunks(
            store, tenant_id=document.tenant_id, item_id=document.node_id, body=document.body,
            acl_groups=params["acl_groups"], min_level=params["min_level"], deleted=document.deleted,
        )
    email = _author_email(document)
    if email:
        link_author(
            graph,
            tenant_id=document.tenant_id,
            item_id=document.node_id,
            email=email,
            name=document.author_name or None,
            github_login=document.author_external_id if document.source == "github" and "@" not in document.author_external_id else None,
            notion_user=(
                document.author_external_id
                if document.source == "notion"
                and document.author_external_id
                and "@" not in document.author_external_id
                else None
            ),
        )
    return UpsertResult(node_id=document.node_id, applied=True)


def _author_email(document: CanonicalDocument) -> str:
    if document.author_email:
        return document.author_email
    if "@" in document.author_external_id:
        return document.author_external_id
    return ""


def _allowed(document: CanonicalDocument):
    if document.source == "github":
        found = allowed_source("github", document.repo_slug, document.tenant_id)
        if found is None:
            return None
        group, policy = found
        return "github", group, policy, github_source_id(document.tenant_id, document.repo_slug)
    if document.source == "notion":
        found = allowed_source("notion", document.workspace_id, document.tenant_id)
        if found is None:
            return None
        group, policy = found
        return "notion", group, policy, notion_source_id(document.tenant_id, document.workspace_id)
    if document.source == "gcal":
        found = allowed_source("gcal", document.workspace_id, document.tenant_id)
        if found is None:
            return None
        group, policy = found
        return "gcal", group, policy, gcal_source_id(document.tenant_id, document.workspace_id)
    if document.source == "hubspot":
        found = allowed_source("hubspot", document.workspace_id, document.tenant_id)
        if found is None:
            return None
        group, policy = found
        return "hubspot", group, policy, hubspot_source_id(document.tenant_id, document.workspace_id)
    found = allowed_source("gdrive", document.workspace_id, document.tenant_id)
    if found is None:
        return None
    group, policy = found
    return "gdrive", group, policy, drive_source_id(document.tenant_id, document.workspace_id)


def _item_label(document: CanonicalDocument) -> str:
    if document.source == "github" and document.kind in {"issue", "pull_request"}:
        return Label.ISSUE
    return Label.DOCUMENT


def _params(document, ingested_at, system, group, policy, source_node) -> dict:
    excerpt = "" if document.deleted else document.excerpt
    if document.kind == "event":
        object_type = "event"
    elif document.kind in {"issue", "pull_request"}:
        object_type = document.kind
    else:
        object_type = "document"
    external = document.repo_slug if system == "github" else document.workspace_id
    return {
        "doc_id": document.node_id,
        "tenant_id": document.tenant_id,
        "source": document.source,
        "connection_id": document.connection_id,
        "connection_node_id": f"{document.tenant_id}:{document.source}:connection:{document.connection_id}",
        "workspace_id": document.workspace_id,
        "external_id": document.external_id,
        "object_type": object_type,
        "title": "" if document.deleted else document.title,
        "occurred_at": to_ts(document.occurred_at),
        "updated_at": to_ts(document.updated_at),
        "version": document.version,
        "deleted": document.deleted,
        "excerpt": excerpt,
        "content_hash": document.content_hash,
        "source_url": document.source_url,
        "visibility": document.visibility,
        "kind": document.kind,
        "status": document.status,
        "author_external_id": document.author_external_id,
        "author_email": document.author_email,
        "parent_external_id": document.parent_external_id or "",
        "now": to_ts(ingested_at),
        "acl_groups": source_acl(group, policy),
        "min_level": policy.min_level,
        "source_id": source_node,
        "source_external_id": external,
        "source_name": policy.name,
        "cascade_policy": policy.cascade_policy,
        "authority": policy.authority,
    }
