"""Print one repository id and its open-issue count. No graph writes."""

import os
import sys
from pathlib import Path

from app.github.client import GitHubRequestError, github_client_from_env


def load_env() -> None:
    path = Path(".env")
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def main() -> int:
    load_env()
    owner = os.environ.get("GITHUB_OWNER", "").strip()
    repo = os.environ.get("GITHUB_REPO", "").strip()
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if not owner or not repo or not token:
        print("GITHUB_TOKEN, GITHUB_OWNER, or GITHUB_REPO is empty")
        return 1
    try:
        client = github_client_from_env()
        payload = client.get_repo(owner, repo).payload
        client.list_issues(owner, repo, "1", None)
    except GitHubRequestError as exc:
        print(f"github_error status={exc.status} error={exc.error}")
        return 1
    except ValueError as exc:
        print(exc)
        return 1
    if not isinstance(payload, dict) or isinstance(payload.get("id"), bool) or not isinstance(payload.get("id"), int):
        print("github_error status=0 error=missing_repo_id")
        return 1
    private = bool(payload.get("private"))
    print(f"repo_id={payload['id']} private={private} open_issues={payload.get('open_issues_count')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
