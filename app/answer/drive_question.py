"""Cited answers over Google Docs in one folder. No model is called."""

from dataclasses import dataclass

_QUERY = """
MATCH (d:Document)
WHERE d.tenant_id = $tenant_id
  AND d.source = 'gdrive'
  AND d.workspace_id = $folder_id
  AND coalesce(d.deleted, false) = false
  AND d.excerpt CONTAINS $phrase
RETURN d.excerpt, d.updated_at, d.source_url
ORDER BY d.version DESC
LIMIT 20
"""


@dataclass(frozen=True)
class DriveCitation:
    excerpt: str
    updated_at: str
    source_url: str


@dataclass(frozen=True)
class DriveAnswer:
    text: str
    citations: tuple[DriveCitation, ...]


def answer_drive_question(graph, tenant_id: str, folder_id: str, phrase: str) -> DriveAnswer:
    if not isinstance(phrase, str) or not phrase.strip():
        raise ValueError("phrase is required")
    rows = graph.query(
        _QUERY,
        {"tenant_id": tenant_id, "folder_id": folder_id, "phrase": phrase},
    ).result_set
    citations = tuple(
        DriveCitation(excerpt=row[0] or "", updated_at=row[1] or "", source_url=row[2] or "")
        for row in rows
    )
    if not citations:
        return DriveAnswer("The graph has no matching Google Doc.", ())
    lines = []
    for citation in citations:
        lines.append(f"\"{citation.excerpt}\"")
        if citation.source_url:
            lines.append(citation.source_url)
    return DriveAnswer("\n".join(lines), citations)
