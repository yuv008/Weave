"""Report connector gates without printing secrets or document text."""

import json
import os
import urllib.error
import urllib.request
from pathlib import Path


def load_env() -> None:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def flag(name: str) -> None:
    value = os.environ.get(name, "")
    print(f"{name}_set={bool(value)} length={len(value)}")


def notion_search_count() -> None:
    token = os.environ.get("NOTION_TOKEN", "")
    version = os.environ.get("NOTION_VERSION") or "2022-06-28"
    if not token:
        print("notion_search=skipped")
        return
    body = json.dumps({"filter": {"property": "object", "value": "page"}, "page_size": 1}).encode()
    request = urllib.request.Request(
        "https://api.notion.com/v1/search",
        data=body,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": version,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            payload = json.loads(response.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"notion_search_status={exc.code}")
        return
    results = payload.get("results") or []
    print(f"notion_search_status=200 page_count_first_page={len(results)} has_more={bool(payload.get('has_more'))}")


def slack_graph_count() -> None:
    try:
        from app.graph.client import connect_graph
    except Exception as exc:
        print(f"graph=unavailable {type(exc).__name__}")
        return
    tenant = os.environ.get("WEAVE_TENANT_ID", "")
    if not tenant:
        print("graph=skipped")
        return
    try:
        graph = connect_graph()
        count = graph.query(
            "MATCH (m:Message) WHERE m.tenant_id = $tenant RETURN count(m)",
            {"tenant": tenant},
        ).result_set[0][0]
    except Exception as exc:
        print(f"graph_error={type(exc).__name__}")
        return
    print(f"slack_graph_messages={count}")


def main() -> None:
    load_env()
    for name in (
        "NOTION_TOKEN",
        "NOTION_VERSION",
        "NOTION_WORKSPACE_ID",
        "NOTION_CONNECTION_ID",
        "GOOGLE_SERVICE_ACCOUNT_FILE",
        "GOOGLE_DRIVE_FOLDER_ID",
        "GOOGLE_DRIVE_CONNECTION_ID",
        "SLACK_BOT_TOKEN",
    ):
        flag(name)
    key_path = os.environ.get("GOOGLE_SERVICE_ACCOUNT_FILE") or "secrets/gdrive-service-account.json"
    print(f"service_account_file_exists={Path(key_path).is_file()}")
    notion_search_count()
    slack_graph_count()


if __name__ == "__main__":
    main()
