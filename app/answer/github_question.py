"""Cited answers over one GitHub repository. No model is called."""

from dataclasses import dataclass

_QUERY = """
MATCH (d)
WHERE (d:Document OR d:Issue)
  AND d.tenant_id = $tenant_id
  AND d.source = 'github'
  AND d.workspace_id = $repo_id
  AND coalesce(d.deleted, false) = false
  AND coalesce(d.placeholder, false) = false
  AND d.excerpt CONTAINS $phrase
RETURN d.excerpt, d.updated_at, d.source_url, d.kind, d.status
ORDER BY d.version DESC
LIMIT 20
"""


@dataclass(frozen=True)
class GitHubCitation:
    excerpt: str
    updated_at: str
    source_url: str
    kind: str
    status: str


@dataclass(frozen=True)
class GitHubAnswer:
    text: str
    citations: tuple[GitHubCitation, ...]


def answer_github_question(graph, tenant_id: str, repo_id: str, phrase: str) -> GitHubAnswer:
    if not isinstance(phrase, str) or not phrase.strip():
        raise ValueError("phrase is required")
    rows = graph.query(
        _QUERY,
        {"tenant_id": tenant_id, "repo_id": repo_id, "phrase": phrase},
    ).result_set
    citations = tuple(
        GitHubCitation(
            excerpt=row[0] or "",
            updated_at=row[1] or "",
            source_url=row[2] or "",
            kind=row[3] or "",
            status=row[4] or "",
        )
        for row in rows
    )
    if not citations:
        return GitHubAnswer("The graph has no matching GitHub issue or comment.", ())
    lines = []
    for citation in citations:
        lines.append(f"\"{citation.excerpt}\"")
        if citation.source_url:
            lines.append(citation.source_url)
    return GitHubAnswer("\n".join(lines), citations)
