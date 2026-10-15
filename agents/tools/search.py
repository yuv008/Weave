"""search_context: hybrid keyword and vector search over the chunks the caller can see.

The gate's SQL predicate sits in the WHERE clause, so filtering happens
before ranking and LIMIT. Hits are re-checked against the graph (the chunk's
ACL copy can lag the item), then expanded one hop to the claims they evidence
and the subjects they mention. Without an embedder the vector half is
skipped and results are keyword-only.
"""

from agents.gate.visible import chunk_visible_sql, gate_params
from agents.tools.base import PathEdge, ToolContext, ToolInputError, ToolResult, tool
from agents.tools.common import item_citations
from shared.acl import SYSTEMS
from shared.schema import CLAIM_LABELS, CONTRACT_LABELS, Label, Prop, Rel

MAX_LIMIT = 20
RRF_K = 60

_KEYWORD = f"""
SELECT chunk.item_id, ts_rank(chunk.tsv, q) AS rank
FROM chunk, to_tsquery('english', replace(plainto_tsquery('english', %(query)s)::text, '&', '|')) AS q
WHERE chunk.tenant_id = %(tenant)s
  AND {chunk_visible_sql("chunk")}
  AND chunk.item_id LIKE %(prefix)s
  AND chunk.tsv @@ q
ORDER BY rank DESC
LIMIT %(limit)s
"""

_VECTOR = f"""
SELECT chunk.item_id, 1 - (chunk.embedding <=> %(vector)s::vector) AS rank
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


def _label(labels: list[str]) -> str:
    return next((label for label in CONTRACT_LABELS if label in labels), labels[0] if labels else "")


@tool(
    "search_context",
    use_when="Nothing more specific fits, or you need messages and documents about a free-text question.",
    dont_use_when="You need the current decision (get_decision), a number (get_figures) or owners (find_owner).",
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
            keyword = [row[0] for row in cur.fetchall()]
            vector = []
            if ctx.embed is not None:
                literal = "[" + ",".join(f"{v:.7f}" for v in ctx.embed(query)) + "]"
                cur.execute(_VECTOR, {**params, "vector": literal})
                vector = [row[0] for row in cur.fetchall()]
    finally:
        conn.close()

    ranked = fuse(keyword, vector)
    citations = {c.item_id: c for c in item_citations(ctx, [item for item, _ in ranked])}
    result = ToolResult("search_context", {"hits": [], "mode": "hybrid" if ctx.embed else "keyword"})
    result.hidden += sum(1 for item, _ in ranked if item not in citations)
    hits = [(item, score) for item, score in ranked if item in citations][:limit]
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
        result.data["hits"].append({
            "item_id": item, "score": round(score, 5), "snippet": cite.quote, "source": cite.source,
            "author": cite.author, "at": cite.at, "url": cite.url,
            "claims": claims[item], "mentions": mentions[item],
        })
        result.node_ids += [item, *(c["id"] for c in claims[item]), *(m["id"] for m in mentions[item])]
        result.citations.append(cite)
        result.path += [PathEdge(c["id"], Rel.EVIDENCED_BY, item) for c in claims[item]]
        result.path += [PathEdge(item, Rel.MENTIONS, m["id"]) for m in mentions[item]]
    if not hits:
        result.message = "Nothing visible to you matches that."
    return result
