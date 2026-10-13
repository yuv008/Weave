"""Cited answers over Notion documents. No model is called."""

from dataclasses import dataclass

_QUERY = """
MATCH (d:Document)
WHERE d.tenant_id = $tenant_id
  AND d.source = 'notion'
  AND coalesce(d.deleted, false) = false
  AND d.excerpt CONTAINS $phrase
RETURN d.excerpt, d.updated_at, d.source_url
ORDER BY d.version DESC
LIMIT 20
"""


@dataclass(frozen=True)
class NotionCitation:
    excerpt: str
    updated_at: str
    source_url: str


@dataclass(frozen=True)
class NotionAnswer:
    text: str
    citations: tuple[NotionCitation, ...]


def answer_notion_question(graph, tenant_id: str, phrase: str) -> NotionAnswer:
    if not isinstance(phrase, str) or not phrase.strip():
        raise ValueError("phrase is required")
    rows = graph.query(_QUERY, {"tenant_id": tenant_id, "phrase": phrase}).result_set
    citations = tuple(
        NotionCitation(excerpt=row[0] or "", updated_at=row[1] or "", source_url=row[2] or "")
        for row in rows
    )
    if not citations:
        return NotionAnswer("The graph has no matching Notion document.", ())
    lines = []
    for citation in citations:
        lines.append(f"\"{citation.excerpt}\"")
        if citation.source_url:
            lines.append(citation.source_url)
    return NotionAnswer("\n".join(lines), citations)
