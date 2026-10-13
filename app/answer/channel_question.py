"""Answer one public-channel question from stored excerpts.

No model is called. A deleted or placeholder message cannot be cited.
"""

from dataclasses import dataclass

_QUERY = """
MATCH (m:Message)
WHERE m.tenant_id = $tenant_id
  AND m.external_id STARTS WITH $channel_prefix
  AND coalesce(m.deleted, false) = false
  AND coalesce(m.placeholder, false) = false
  AND m.excerpt CONTAINS $phrase
RETURN m.excerpt, m.occurred_at, m.source_url
ORDER BY m.occurred_at DESC
LIMIT 20
"""


@dataclass(frozen=True)
class Citation:
    excerpt: str
    occurred_at: str
    source_url: str


@dataclass(frozen=True)
class ChannelAnswer:
    text: str
    citations: tuple[Citation, ...]


def answer_channel_question(graph, tenant_id: str, channel_id: str, phrase: str) -> ChannelAnswer:
    if not isinstance(phrase, str) or not phrase.strip():
        raise ValueError("phrase is required")
    rows = graph.query(
        _QUERY,
        {
            "tenant_id": tenant_id,
            "channel_prefix": f"{channel_id}:",
            "phrase": phrase,
        },
    ).result_set
    citations = tuple(
        Citation(
            excerpt=row[0] or "",
            occurred_at=row[1] or "",
            source_url=row[2] or "",
        )
        for row in rows
    )
    if not citations:
        return ChannelAnswer("The graph has no matching public message.", ())
    lines: list[str] = []
    for citation in citations:
        lines.append(f"\"{citation.excerpt}\"")
        if citation.source_url:
            lines.append(citation.source_url)
    return ChannelAnswer("\n".join(lines), citations)
