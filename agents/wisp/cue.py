"""One hint for a prompt the caller may see.

Company graph first (``search_context``), then a personal memory note
(``recall``, audience ``person`` only). A company citation wins when both
match. The prompt is search input only: this module does not call the answer
loop or a model, and it does not write episodes, conversation turns, or logs.
"""

import threading

from agents.gate.visible import chunk_visible_sql, gate_params
from agents.memory.recall import namespace, recall
from agents.memory.store import Episode, terms
from agents.tools.base import ToolContext
from agents.tools.common import visible_node_ids
from agents.tools.search import search_context
from shared.acl import SYSTEMS

QUOTE_CHARS = 180
PERSONAL_SOURCE = "Personal note"
_RECALL_LIMIT = 5
_DB_LOCK = threading.Lock()
_DB: dict[int, object] = {}


def cue(ctx: ToolContext, prompt: str) -> dict[str, str]:
    """The single best hit for ``prompt``, or ``unsure`` with empty strings."""

    company = _company_hit(ctx, prompt)
    if company is not None:
        return company
    personal = _personal_hit(ctx, prompt)
    if personal is not None:
        return personal
    return _unsure()


def excerpt(text: str | None, limit: int = QUOTE_CHARS) -> str:
    """Whitespace-collapsed stored text, cut at ``limit`` characters."""

    collapsed = " ".join((text or "").replace("\ufeff", "").split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[:limit].rstrip()


def _unsure() -> dict[str, str]:
    return {"state": "unsure", "summary": "", "quote": "", "source": "", "item_id": ""}


def _found(summary: str, quote: str, source: str, item_id: str) -> dict[str, str]:
    return {"state": "found", "summary": summary, "quote": quote, "source": source, "item_id": item_id}


_CHUNK_TEXT = f"""
SELECT chunk.item_id, chunk.text
FROM chunk, to_tsquery('english', replace(plainto_tsquery('english', %(query)s)::text, '&', '|')) AS q
WHERE chunk.tenant_id = %(tenant)s
  AND {chunk_visible_sql("chunk")}
  AND chunk.item_id LIKE %(prefix)s
  AND chunk.tsv @@ q
ORDER BY ts_rank(chunk.tsv, q) DESC
LIMIT 1
"""


def _company_hit(ctx: ToolContext, prompt: str) -> dict[str, str] | None:
    row = _chunk_row(ctx, prompt)
    if row is not None and len(row) >= 2 and str(row[1] or "").strip():
        source = _source_from_id(str(row[0]))
        summary = f"This came up in {source}" if source else "This came up in a company source"
        return _found(summary, excerpt(row[1]), source, str(row[0]))
    if row is None or len(row) >= 2:
        return None
    try:
        result = search_context(ctx, prompt, limit=1)
    except Exception:
        return None
    return _from_search(result)


def _from_search(result) -> dict[str, str] | None:
    hits = result.data.get("hits") or []
    if not hits:
        return None
    hit = hits[0]
    item_id = hit.get("item_id") or ""
    if not item_id:
        return None
    cite = next((c for c in result.citations if getattr(c, "item_id", None) == item_id), None)
    raw_source = getattr(cite, "source", None) if cite is not None else None
    if not raw_source:
        raw_source = hit.get("source")
    source = str(raw_source).strip() if raw_source else ""
    quote = getattr(cite, "quote", None) if cite is not None else None
    if quote is None:
        quote = hit.get("snippet")
    summary = f"This came up in {source}" if source else "This came up in a company source"
    return _found(summary, excerpt(quote), source, item_id)


def _source_from_id(item_id: str) -> str:
    parts = item_id.split(":")
    if len(parts) >= 2 and parts[1] in SYSTEMS:
        return parts[1]
    return ""


def _reuse(connect):
    """One autocommit connection per connect function. Test doubles are not kept."""

    key = id(connect)
    with _DB_LOCK:
        cached = _DB.get(key)
        if cached is not None and not getattr(cached, "closed", True):
            return cached, True
    conn = connect()
    if not hasattr(conn, "autocommit"):
        return conn, False
    conn.autocommit = True
    with _DB_LOCK:
        _DB[key] = conn
    return conn, True


def _drop(connect) -> None:
    key = id(connect)
    with _DB_LOCK:
        conn = _DB.pop(key, None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass


def _chunk_row(ctx: ToolContext, prompt: str):
    """One visible chunk, or None. A one-column row means the caller should use the graph."""

    if ctx.pg_connect is None:
        return None
    tenant = ctx.principal.tenant
    params = {**gate_params(ctx.principal), "tenant": tenant, "query": prompt, "prefix": f"{tenant}:%"}
    conn, keep = _reuse(ctx.pg_connect)
    try:
        with conn.cursor() as cur:
            cur.execute(_CHUNK_TEXT, params)
            rows = cur.fetchall()
    except Exception:
        if keep:
            _drop(ctx.pg_connect)
        return None
    finally:
        if not keep:
            try:
                conn.close()
            except Exception:
                pass
    return rows[0] if rows else None


_EPISODES = """
SELECT id, note, node_ids
FROM episodes, to_tsquery('english', %(query)s) AS q
WHERE tenant_id = %(tenant)s
  AND person_id = %(person)s
  AND agent = %(agent)s
  AND audience = 'person'
  AND tsv @@ q
ORDER BY ts_rank(tsv, q) DESC, created_at DESC
LIMIT 5
"""

_RECENT_EPISODES = """
SELECT id, note, node_ids
FROM episodes
WHERE tenant_id = %(tenant)s
  AND person_id = %(person)s
  AND agent = %(agent)s
  AND audience = 'person'
ORDER BY created_at DESC
LIMIT 5
"""


def _personal_hit(ctx: ToolContext, prompt: str) -> dict[str, str] | None:
    loaded = _fast_episodes(ctx, prompt)
    episodes = recall(ctx, prompt, _RECALL_LIMIT) if loaded is None else loaded
    for episode in episodes:
        if episode.audience != "person" or not episode.id:
            continue
        quote = excerpt(episode.note)
        if not quote:
            continue
        return _found("This came up in a personal note", quote, PERSONAL_SOURCE, episode.id)
    return None


def _kept(connect):
    """The cue's open autocommit connection, if this caller already has one."""

    with _DB_LOCK:
        cached = _DB.get(id(connect))
    if cached is not None and not getattr(cached, "closed", True):
        return cached
    return None


def _fast_episodes(ctx: ToolContext, prompt: str) -> list[Episode] | None:
    """Personal notes on the connection the company lookup already opened.

    Opening a second connection, then switching role, is several round trips.
    Offline stores have no kept connection and use ``recall`` instead.
    """

    if ctx.pg_connect is None:
        return None
    conn = _kept(ctx.pg_connect)
    ns = namespace(ctx)
    if conn is None or ns is None:
        return None
    words = terms(prompt)
    params = {"tenant": ns.tenant, "person": ns.person_id, "agent": ns.agent, "query": " | ".join(words)}
    try:
        with conn.cursor() as cur:
            cur.execute(_EPISODES if words else _RECENT_EPISODES, params)
            rows = cur.fetchall()
    except Exception:
        _drop(ctx.pg_connect)
        return []
    found = [
        Episode(ns.tenant, ns.person_id, ns.agent, note=note or "", node_ids=list(node_ids or []), id=episode_id)
        for episode_id, note, node_ids in rows
        if episode_id
    ]
    visible = visible_node_ids(ctx, [node for episode in found for node in episode.node_ids])
    return [episode for episode in found if all(node in visible for node in episode.node_ids)]
