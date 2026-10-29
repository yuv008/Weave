"""search_context: hybrid keyword and vector search over the chunks the caller can see.

The gate's SQL predicate sits in the WHERE clause, so filtering happens
before ranking and LIMIT. Hits are re-checked against the graph (the chunk's
ACL copy can lag the item), then expanded one hop to the claims they evidence
and the subjects they mention. Without an embedder the vector half is
skipped and results are keyword-only.

An item may have many chunks: it is ranked by its best one, which becomes the
snippet. Hits with the same text collapse into the newest copy, listing the
others as ``similar``. When relevance is within ``TIE`` of the next hit, the
source's authority and then recency break the tie.
"""

from agents.gate.visible import chunk_visible_sql, gate_params
from agents.tools.base import PathEdge, ToolContext, ToolInputError, ToolResult, tool
from agents.tools.common import item_citations
from shared.acl import SYSTEMS
from shared.schema import CLAIM_LABELS, CONTRACT_LABELS, Label, Prop, Rel

MAX_LIMIT = 20
RRF_K = 60
TIE = 0.001
SNIPPET_CHARS = 400
_EMPTY_HASH = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"

_KEYWORD = f"""
SELECT chunk.item_id, ts_rank(chunk.tsv, q) AS rank, chunk.text
FROM chunk, to_tsquery('english', replace(plainto_tsquery('english', %(query)s)::text, '&', '|')) AS q
WHERE chunk.tenant_id = %(tenant)s
  AND {chunk_visible_sql("chunk")}
  AND chunk.item_id LIKE %(prefix)s
  AND chunk.tsv @@ q
ORDER BY rank DESC
LIMIT %(limit)s
"""

_VECTOR = f"""
SELECT chunk.item_id, 1 - (chunk.embedding <=> %(vector)s::vector) AS rank, chunk.text
FROM chunk
WHERE chunk.tenant_id = %(tenant)s
  AND {chunk_visible_sql("chunk")}
  AND chunk.item_id LIKE %(prefix)s
  AND chunk.embedding IS NOT NULL
ORDER BY chunk.embedding <=> %(vector)s::vector
LIMIT %(limit)s
"""

_CLAIMS = f"""
UNWIND $ids AS iid
MATCH (c)-[:{Rel.EVIDENCED_BY}]->(i:{Label.ITEM} {{{Prop.ID}: iid}})
WHERE {" OR ".join(f"c:{label}" for label in CLAIM_LABELS)}
RETURN iid, c.{Prop.ID}, labels(c), coalesce(c.{Prop.STATEMENT}, c.{Prop.NAME}), c.{Prop.STATUS}
"""

_META = f"""
UNWIND $ids AS iid
MATCH (i:{Label.ITEM} {{{Prop.ID}: iid}})
OPTIONAL MATCH (i)-[:{Rel.IN_SOURCE}]->(s:{Label.SOURCE})
RETURN iid, i.{Prop.CONTENT_HASH}, i.{Prop.OCCURRED_AT}, max(coalesce(s.{Prop.AUTHORITY}, 0))
"""

_MENTIONS = f"""
UNWIND $ids AS iid
MATCH (i:{Label.ITEM} {{{Prop.ID}: iid}})-[:{Rel.MENTIONS}]->(s)
RETURN iid, s.{Prop.ID}, labels(s), s.{Prop.NAME}
"""


def fuse(*rankings: list[str]) -> list[tuple[str, float]]:
    """Reciprocal rank fusion of ranked id lists."""

    scores: dict[str, float] = {}
    for ranking in rankings:
        for position, item in enumerate(ranking):
            scores[item] = scores.get(item, 0.0) + 1.0 / (RRF_K + position + 1)
    return sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))


def best_chunks(rows: list[tuple]) -> tuple[list[str], dict[str, str]]:
    """Item ids in rank order, each once, and the text of each item's best chunk."""

    order: list[str] = []
    text: dict[str, str] = {}
    for item, _rank, chunk in rows:
        if item not in text:
            order.append(item)
            text[item] = chunk or ""
    return order, text


def arrange(ranked: list[tuple[str, float]], meta: dict, snippets: dict[str, str]) -> list[dict]:
    """Break near-ties by authority then recency, and fold duplicate text into the newest copy."""

    def info(item):
        content_hash, at, authority = meta.get(item, ("", "", 0))
        return content_hash, str(at or ""), authority or 0

    # Two stable sorts: newest first, then by relevance bucket and authority.
    ordered = sorted(ranked, key=lambda e: info(e[0])[1], reverse=True)
    ordered = sorted(ordered, key=lambda e: (-round(e[1] / TIE), -info(e[0])[2]))
    groups: dict[str, list[tuple[str, float]]] = {}
    for item, score in ordered:
        content_hash = info(item)[0]
        same = content_hash if content_hash and content_hash != _EMPTY_HASH else " ".join(
            (snippets.get(item) or item).lower().split())[:200]
        groups.setdefault(same, []).append((item, score))
    hits = []
    for members in groups.values():
        newest = max(members, key=lambda e: info(e[0])[1])
        hits.append({"item": newest[0], "score": max(score for _, score in members),
                     "similar": [item for item, _ in members if item != newest[0]]})
    return hits


def _label(labels: list[str]) -> str:
    return next((label for label in CONTRACT_LABELS if label in labels), labels[0] if labels else "")


@tool(
    "search_context",
    use_when="Nothing more specific fits, or you need messages and documents about a free-text question.",
    dont_use_when="You need the current decision (get_decision), a number (get_figures) or owners (find_owner).",
    args={
        "query": "Free-text question or keywords.",
        "system": "Optional source system to search only, e.g. 'slack' or 'github'.",
        "limit": f"Maximum hits to return, 1 to {MAX_LIMIT}.",
    },
    choices={"system": tuple(SYSTEMS)},
)
def search_context(ctx: ToolContext, query: str, system: str | None = None, limit: int = 8) -> ToolResult:
    """Hybrid hits with citations, each expanded one hop to its claims and mentioned subjects."""

    if not query or not query.strip():
        raise ToolInputError("give a search query")
    if system is not None and system not in SYSTEMS:
        raise ToolInputError(f"system must be one of {list(SYSTEMS)}")
    if ctx.pg_connect is None:
        raise RuntimeError("search_context needs a Postgres connection")
    limit = max(1, min(int(limit), MAX_LIMIT))
    tenant = ctx.principal.tenant
    params = {
        **gate_params(ctx.principal),
        "tenant": tenant,
        "query": query,
        "prefix": f"{tenant}:{system}:%" if system else f"{tenant}:%",
        "limit": limit * 2,
    }
    conn = ctx.pg_connect()
    try:
        with conn.cursor() as cur:
            cur.execute(_KEYWORD, params)
            keyword, snippets = best_chunks(cur.fetchall())
            vector = []
            if ctx.embed is not None:
                literal = "[" + ",".join(f"{v:.7f}" for v in ctx.embed(query)) + "]"
                cur.execute(_VECTOR, {**params, "vector": literal})
                vector, vector_text = best_chunks(cur.fetchall())
                for item, text in vector_text.items():
                    snippets.setdefault(item, text)
    finally:
        conn.close()

    ranked = fuse(keyword, vector)
    citations = {c.item_id: c for c in item_citations(ctx, [item for item, _ in ranked])}
    result = ToolResult("search_context", {"hits": [], "mode": "hybrid" if ctx.embed else "keyword"})
    result.hidden += sum(1 for item, _ in ranked if item not in citations)
    visible = [(item, score) for item, score in ranked if item in citations]
    meta = {row[0]: (row[1], row[2], row[3]) for row in ctx.rows(_META, ids=[item for item, _ in visible])} if visible else {}
    arranged = arrange(visible, meta, snippets)[:limit]
    hits = [(hit["item"], hit["score"]) for hit in arranged]
    similar = {hit["item"]: hit["similar"] for hit in arranged}
    ids = [item for item, _ in hits]

    claims: dict[str, list[dict]] = {item: [] for item in ids}
    mentions: dict[str, list[dict]] = {item: [] for item in ids}
    if ids:
        for item, claim, labels, text, status in ctx.rows(_CLAIMS, ids=ids):
            claims[item].append({"id": claim, "label": _label(labels), "text": text, "status": status})
        for item, subject, labels, name in ctx.rows(_MENTIONS, ids=ids):
            mentions[item].append({"id": subject, "label": _label(labels), "name": name})

    for item, score in hits:
        cite = citations[item]
        snippet = " ".join((snippets.get(item) or cite.quote or "").split())[:SNIPPET_CHARS]
        result.data["hits"].append({
            "item_id": item, "score": round(score, 5), "snippet": snippet, "source": cite.source,
            "author": cite.author, "at": cite.at, "url": cite.url, "authority": meta.get(item, ("", "", 0))[2],
            "claims": claims[item], "mentions": mentions[item],
            "similar": [
                {"item_id": other, "source": citations[other].source, "at": citations[other].at, "url": citations[other].url}
                for other in similar[item]
            ],
        })
        result.node_ids += [item, *(c["id"] for c in claims[item]), *(m["id"] for m in mentions[item])]
        result.citations.append(cite)
        result.path += [PathEdge(c["id"], Rel.EVIDENCED_BY, item) for c in claims[item]]
        result.path += [PathEdge(item, Rel.MENTIONS, m["id"]) for m in mentions[item]]
    if not hits:
        result.message = "Nothing visible to you matches that."
    return result
