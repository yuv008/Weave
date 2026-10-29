"""GitHub webhook handling for one repository.

The signature is checked by the caller before this module reads the payload
as a command. A delivery for any other repository is ignored.
"""

import hashlib
import hmac
import logging

from app.github.normalize import is_bot, normalize_github_comment, normalize_github_issue
from app.graph.upsert_document import upsert_document

logger = logging.getLogger("weave.github.events")

_ISSUE_ACTIONS = {"opened", "edited", "closed", "reopened", "deleted"}
_PULL_ACTIONS = {"opened", "edited", "closed", "reopened"}
_COMMENT_ACTIONS = {"created", "edited", "deleted"}

_SEEN = """
MATCH (g:GithubDelivery {id: $id})
RETURN g.id
"""

_MARK = """
CREATE (:GithubDelivery {id: $id, tenant_id: $tenant_id})
"""


def verify_github_signature(secret: str, body: bytes, signature: str) -> bool:
    if not secret or not signature or not signature.startswith("sha256="):
        return False
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(f"sha256={digest}", signature)


def apply_github_event(
    payload: dict,
    graph,
    *,
    event: str,
    tenant_id: str,
    connection_id: str,
    owner: str,
    repo: str,
    delivery_id: str,
) -> dict:
    repository = payload.get("repository")
    if not _same_repo(repository, owner, repo):
        logger.info("github_event_skip event=%s", event or payload.get("action") or "")
        return {"ok": True}
    repo_id = repository.get("id")
    if isinstance(repo_id, bool) or not isinstance(repo_id, int):
        return {"ok": True}
    action = payload.get("action") if isinstance(payload.get("action"), str) else ""
    if not _accepted(event, action):
        logger.info("github_event_skip event=%s", event or action)
        return {"ok": True}
    scoped = f"{tenant_id}:github:delivery:{delivery_id}" if delivery_id else ""
    if scoped and graph.query(_SEEN, {"id": scoped}).result_set:
        return {"ok": True}
    document = _document(payload, event, action, tenant_id, connection_id, str(repo_id), owner, repo)
    if document is None:
        return {"ok": True}
    upsert_document(graph, document)
    if scoped:
        graph.query(_MARK, {"id": scoped, "tenant_id": tenant_id})
    return {"ok": True}


def _document(payload, event, action, tenant_id, connection_id, repo_id, owner, repo):
    deleted = action == "deleted"
    if event == "issue_comment":
        comment = payload.get("comment")
        issue = payload.get("issue")
        number = issue.get("number") if isinstance(issue, dict) else None
        if not isinstance(comment, dict) or is_bot(comment):
            return None
        return normalize_github_comment(
            comment,
            tenant_id=tenant_id,
            connection_id=connection_id,
            repo_id=repo_id,
            issue_number=number,
            owner=owner,
            repo=repo,
            deleted=deleted,
        )
    if event == "pull_request":
        pull = payload.get("pull_request")
        if not isinstance(pull, dict) or is_bot(pull):
            return None
        return normalize_github_issue(
            pull,
            tenant_id=tenant_id,
            connection_id=connection_id,
            repo_id=repo_id,
            owner=owner,
            repo=repo,
            kind="pull_request",
        )
    issue = payload.get("issue")
    if not isinstance(issue, dict) or is_bot(issue):
        return None
    return normalize_github_issue(
        issue,
        tenant_id=tenant_id,
        connection_id=connection_id,
        repo_id=repo_id,
        owner=owner,
        repo=repo,
        deleted=deleted,
    )


def _accepted(event: str, action: str) -> bool:
    if event == "issues":
        return action in _ISSUE_ACTIONS
    if event == "pull_request":
        return action in _PULL_ACTIONS
    if event == "issue_comment":
        return action in _COMMENT_ACTIONS
    return False


def _same_repo(repository, owner: str, repo: str) -> bool:
    if not isinstance(repository, dict):
        return False
    login = repository.get("owner")
    login_name = login.get("login") if isinstance(login, dict) else ""
    name = repository.get("name")
    if not isinstance(login_name, str) or not isinstance(name, str):
        return False
    return login_name.lower() == owner.lower() and name.lower() == repo.lower()
