"""Map one GitHub issue, pull request, or issue comment into a document."""

import hashlib

from app.ingest.time import parse_utc, sortable_timestamp
from app.models.canonical import EXCERPT_LIMIT
from app.models.canonical_document import CanonicalDocument


def normalize_github_issue(
    issue: dict,
    *,
    tenant_id: str,
    connection_id: str,
    repo_id: str,
    owner: str = "",
    repo: str = "",
    kind: str | None = None,
    deleted: bool = False,
) -> CanonicalDocument:
    if not isinstance(issue, dict):
        raise ValueError("GitHub issue must be an object")
    number = _number(issue.get("number"), "GitHub issue number is required")
    updated = _required_text(issue.get("updated_at"), "GitHub updated_at is required")
    created = issue.get("created_at") if isinstance(issue.get("created_at"), str) else updated
    resolved_kind = kind or ("pull_request" if isinstance(issue.get("pull_request"), dict) else "issue")
    if resolved_kind not in {"issue", "pull_request"}:
        raise ValueError("GitHub issue kind is invalid")
    title = issue.get("title") if isinstance(issue.get("title"), str) else ""
    body = issue.get("body") if isinstance(issue.get("body"), str) else ""
    text = "" if deleted else _join(title, body)
    url = issue.get("html_url") if isinstance(issue.get("html_url"), str) else ""
    return CanonicalDocument(
        tenant_id=tenant_id,
        connection_id=connection_id,
        source="github",
        workspace_id=_repo_id(repo_id),
        external_id=f"issue:{number}",
        title="" if deleted else title[:200],
        occurred_at=parse_utc(created),
        updated_at=parse_utc(updated),
        version=_version(updated, deleted=deleted),
        deleted=deleted,
        source_url=url,
        excerpt="" if deleted else text[:EXCERPT_LIMIT],
        content_hash=_hash("" if deleted else text),
        kind=resolved_kind,
        status="" if deleted else _status(issue, resolved_kind),
        author_external_id=_login(issue),
        repo_slug=_slug(owner, repo),
    )


def normalize_github_comment(
    comment: dict,
    *,
    tenant_id: str,
    connection_id: str,
    repo_id: str,
    issue_number: int | str,
    owner: str = "",
    repo: str = "",
    deleted: bool = False,
) -> CanonicalDocument:
    if not isinstance(comment, dict):
        raise ValueError("GitHub comment must be an object")
    comment_id = _number(comment.get("id"), "GitHub comment id is required")
    parent = _number(issue_number, "GitHub issue number is required")
    updated = _required_text(comment.get("updated_at"), "GitHub updated_at is required")
    created = comment.get("created_at") if isinstance(comment.get("created_at"), str) else updated
    body = comment.get("body") if isinstance(comment.get("body"), str) else ""
    text = "" if deleted else body
    url = comment.get("html_url") if isinstance(comment.get("html_url"), str) else ""
    return CanonicalDocument(
        tenant_id=tenant_id,
        connection_id=connection_id,
        source="github",
        workspace_id=_repo_id(repo_id),
        external_id=f"comment:{comment_id}",
        title="",
        occurred_at=parse_utc(created),
        updated_at=parse_utc(updated),
        version=_version(updated, deleted=deleted),
        deleted=deleted,
        source_url=url,
        excerpt="" if deleted else text[:EXCERPT_LIMIT],
        content_hash=_hash(text),
        kind="comment",
        status="",
        author_external_id=_login(comment),
        parent_external_id=f"issue:{parent}",
        repo_slug=_slug(owner, repo),
    )


def _status(issue: dict, kind: str) -> str:
    if kind == "pull_request":
        pull = issue.get("pull_request") if isinstance(issue.get("pull_request"), dict) else {}
        merged_at = pull.get("merged_at") if isinstance(pull, dict) else None
        if not isinstance(merged_at, str) or not merged_at:
            merged_at = issue.get("merged_at")
        if (isinstance(merged_at, str) and merged_at) or issue.get("merged") is True:
            return "merged"
    if issue.get("state") == "closed":
        return "closed"
    return "open"


def _version(updated_at: str, *, deleted: bool) -> str:
    stamped = sortable_timestamp(updated_at)
    if deleted:
        return f"{stamped}~deleted"
    return stamped


def _join(title: str, body: str) -> str:
    if title and body:
        return f"{title}\n{body}"
    return title or body


def _hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _login(payload: dict) -> str:
    user = payload.get("user")
    if isinstance(user, dict) and isinstance(user.get("login"), str):
        return user["login"]
    return ""


def _slug(owner: str, repo: str) -> str:
    if owner and repo:
        return f"{owner}/{repo}"
    return ""


def _repo_id(value: str) -> str:
    if not isinstance(value, str) or not value.isdigit():
        raise ValueError("repo id must be numeric")
    return value


def _number(value: object, message: str) -> int:
    if isinstance(value, bool):
        raise ValueError(message)
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    raise ValueError(message)


def _required_text(value: object, message: str) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError(message)
    return value
