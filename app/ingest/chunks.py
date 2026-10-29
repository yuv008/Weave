"""Chunk an item's full text when it changes, and embed pending chunks in the worker.

Ingest writes chunks with no embedding, so keyword search finds them at once
and the edge (which has no embedding model) can ingest too. The worker embeds
what is still pending after each page. An unchanged item is never re-chunked
or re-embedded. Tests pass an embedder and a memory store.
"""

from shared.config import load_config

CHUNK_CHARS = 1200
OVERLAP_CHARS = 150
# ponytail: one cap for every source; a per-source limit if a real customer needs more.
BODY_LIMIT = 100_000
_model = None


def split_text(text: str, size: int = CHUNK_CHARS, overlap: int = OVERLAP_CHARS) -> list[str]:
    """Pieces of about ``size`` characters, cut at a paragraph, line, or space when one is near."""

    text = text.strip()
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            window = text[start + size // 2:end]
            for mark in ("\n\n", "\n", ". ", " "):
                cut = window.rfind(mark)
                if cut != -1:
                    end = start + size // 2 + cut + len(mark)
                    break
        piece = text[start:end].strip()
        if piece:
            chunks.append(piece)
        if end >= len(text):
            break
        start = max(end - overlap, start + 1)
    return chunks


def write_item_chunks(store, *, tenant_id: str, item_id: str, body: str, acl_groups: list[str], min_level: int,
                      deleted: bool) -> int:
    """Replace one item's chunks with its current text. A deleted or empty item keeps none."""

    text = "" if deleted else (body or "")[:BODY_LIMIT]
    pieces = split_text(text)
    if not pieces:
        store.delete_chunks([item_id])
        return 0
    store.replace_item_chunks(item_id, [
        {
            "id": f"{item_id}:chunk:{ordinal}",
            "tenant_id": tenant_id,
            "item_id": item_id,
            "ordinal": ordinal,
            "text": piece,
            "acl_groups": list(acl_groups),
            "min_level": int(min_level or 0),
        }
        for ordinal, piece in enumerate(pieces)
    ])
    return len(pieces)


def embed_pending(store, tenant_id: str, *, embedder=None, limit: int = 256) -> int:
    """Embed this company's chunks that have no vector yet. Returns how many were embedded."""

    from app.ingest.records import PostgresStore

    if embedder is None and not isinstance(store, PostgresStore):
        return 0
    rows = store.pending_chunks(tenant_id, limit)
    if not rows:
        return 0
    vectors = (embedder or _contract_embedder())([text for _, text in rows])
    store.set_embeddings([(chunk_id, vector) for (chunk_id, _), vector in zip(rows, vectors)])
    return len(rows)


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
