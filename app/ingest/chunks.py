"""Embed item excerpts into the chunk table after a successful poll page.

Tests pass an embedder and a memory store. A live Postgres store loads the
contract model once per process.
"""

from shared.config import load_config
from shared.schema import Label, Prop, Rel

_model = None


def index_source(graph, store, source_id: str, *, embedder=None) -> int:
    """Refresh chunks for every item in one source. Returns the number written."""

    from app.ingest.records import PostgresStore

    if embedder is None and not isinstance(store, PostgresStore):
        return 0
    rows = graph.query(
        f"""
        MATCH (n:{Label.ITEM})-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE} {{{Prop.ID}: $id}})
        RETURN n.{Prop.ID}, n.{Prop.TENANT_ID}, n.{Prop.EXCERPT}, coalesce(n.{Prop.DELETED}, false),
               n.{Prop.ACL_GROUPS}, n.{Prop.MIN_LEVEL}
        """,
        {"id": source_id},
    ).result_set
    keep: list[dict] = []
    drop: list[str] = []
    for item_id, tenant_id, excerpt, deleted, groups, level in rows:
        text = "" if deleted or not excerpt else str(excerpt)
        if not text:
            drop.append(item_id)
            continue
        keep.append(
            {
                "id": f"{item_id}:chunk:0",
                "tenant_id": tenant_id,
                "item_id": item_id,
                "text": text,
                "acl_groups": list(groups or []),
                "min_level": int(level or 0),
            }
        )
    if keep:
        vectors = (embedder or _contract_embedder())([row["text"] for row in keep])
        for row, vector in zip(keep, vectors):
            row["embedding"] = vector
        store.replace_chunks(keep)
    if drop:
        store.delete_chunks(drop)
    return len(keep)


def _contract_embedder():
    def embed(texts: list[str]) -> list[list[float]]:
        global _model
        if _model is None:
            from sentence_transformers import SentenceTransformer

            cfg = load_config()
            _model = SentenceTransformer(cfg.embedding.model)
        vectors = _model.encode(texts, normalize_embeddings=True)
        return [vector.tolist() for vector in vectors]

    return embed
