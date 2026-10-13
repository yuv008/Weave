"""Idempotent FalkorDB upsert for one canonical document.

Relationship direction:
- ``(Tenant)-[:HAS_CONNECTION]->(Connection)`` is written by the worker
- ``(Connection)-[:HAS_SOURCE]->(Document)``

An equal version keeps the stored excerpt and re-merges the edge. An older
version does not write. Text leaves this function only as a parameter.
"""

from datetime import UTC, datetime

from app.models.canonical_document import CanonicalDocument

_APPLY = """
MERGE (d:Document {id: $doc_id})
ON CREATE SET d.version = ''
WITH d,
  CASE
    WHEN coalesce(d.version, '') < $version THEN true
    ELSE false
  END AS do_apply
WHERE coalesce(d.version, '') < $version OR d.version = $version
SET
  d.tenant_id = CASE WHEN do_apply THEN $tenant_id ELSE d.tenant_id END,
  d.source = CASE WHEN do_apply THEN $source ELSE d.source END,
  d.connection_id = CASE WHEN do_apply THEN $connection_id ELSE d.connection_id END,
  d.workspace_id = CASE WHEN do_apply THEN $workspace_id ELSE d.workspace_id END,
  d.external_id = CASE WHEN do_apply THEN $external_id ELSE d.external_id END,
  d.object_type = CASE WHEN do_apply THEN 'document' ELSE d.object_type END,
  d.title = CASE WHEN do_apply THEN $title ELSE d.title END,
  d.occurred_at = CASE WHEN do_apply THEN $occurred_at ELSE d.occurred_at END,
  d.updated_at = CASE WHEN do_apply THEN $updated_at ELSE d.updated_at END,
  d.version = CASE WHEN do_apply THEN $version ELSE d.version END,
  d.deleted = CASE WHEN do_apply THEN $deleted ELSE d.deleted END,
  d.excerpt = CASE WHEN do_apply THEN $excerpt ELSE d.excerpt END,
  d.content_hash = CASE WHEN do_apply THEN $content_hash ELSE d.content_hash END,
  d.source_url = CASE WHEN do_apply THEN $source_url ELSE d.source_url END,
  d.visibility = CASE WHEN do_apply THEN $visibility ELSE d.visibility END,
  d.ingested_at = CASE WHEN do_apply THEN $now ELSE d.ingested_at END
RETURN do_apply
"""

_LINK = """
MERGE (c:Connection {id: $connection_node_id})
ON CREATE SET
  c.tenant_id = $tenant_id,
  c.connection_id = $connection_id,
  c.source = $source,
  c.workspace_id = $workspace_id
MERGE (d:Document {id: $doc_id})
MERGE (c)-[:HAS_SOURCE]->(d)
"""


def upsert_document(graph, document: CanonicalDocument, *, now: datetime | None = None):
    from app.graph.upsert import UpsertResult

    ingested_at = now or datetime.now(UTC)
    if ingested_at.tzinfo is None:
        ingested_at = ingested_at.replace(tzinfo=UTC)
    excerpt = "" if document.deleted else document.excerpt
    params = {
        "doc_id": document.node_id,
        "tenant_id": document.tenant_id,
        "source": document.source,
        "connection_id": document.connection_id,
        "connection_node_id": f"{document.tenant_id}:{document.source}:connection:{document.connection_id}",
        "workspace_id": document.workspace_id,
        "external_id": document.external_id,
        "title": "" if document.deleted else document.title,
        "occurred_at": _iso(document.occurred_at),
        "updated_at": _iso(document.updated_at),
        "version": document.version,
        "deleted": document.deleted,
        "excerpt": excerpt,
        "content_hash": document.content_hash,
        "source_url": document.source_url,
        "visibility": document.visibility,
        "now": _iso(ingested_at),
    }
    rows = graph.query(_APPLY, params).result_set
    if not rows:
        return UpsertResult(node_id=document.node_id, applied=False)
    graph.query(_LINK, params)
    return UpsertResult(node_id=document.node_id, applied=True)


def _iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat()
