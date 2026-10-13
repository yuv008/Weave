"""Run one Notion poll and the Drive access check. Prints counts only."""

import os
from pathlib import Path

from app.gdrive.client import DriveRequestError, drive_client_from_env
from app.gdrive.worker import poll_folder
from app.graph.client import connect_graph
from app.notion.client import PINNED_NOTION_VERSION, notion_client_from_env
from app.notion.worker import poll_shared_pages


def load_env() -> None:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")
    if not os.environ.get("NOTION_VERSION", "").strip():
        os.environ["NOTION_VERSION"] = PINNED_NOTION_VERSION


def main() -> None:
    load_env()
    graph = connect_graph()
    poll_shared_pages(
        os.environ.get("WEAVE_TENANT_ID", "demo"),
        os.environ.get("NOTION_CONNECTION_ID", "notion-main"),
        os.environ["NOTION_WORKSPACE_ID"],
        notion_client_from_env(),
        graph,
    )
    notion_count = graph.query(
        "MATCH (d:Document) WHERE d.source = 'notion' AND d.tenant_id = $tenant RETURN count(d)",
        {"tenant": os.environ.get("WEAVE_TENANT_ID", "demo")},
    ).result_set[0][0]
    print(f"notion_documents={notion_count}")
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        print("drive_status=folder_id_missing")
        return
    client = drive_client_from_env()
    try:
        page = client.list_files(folder_id)
    except DriveRequestError as exc:
        print(f"drive_status={exc.status} drive_error={exc.error}")
        return
    files = page.get("files") or []
    docs = [item for item in files if isinstance(item, dict) and item.get("mimeType", "").endswith("document")]
    print(f"drive_files={len(files)} drive_docs={len(docs)}")
    poll_folder(
        os.environ.get("WEAVE_TENANT_ID", "demo"),
        os.environ.get("GOOGLE_DRIVE_CONNECTION_ID", "gdrive-main"),
        folder_id,
        client,
        graph,
    )
    drive_count = graph.query(
        """
        MATCH (d:Document)
        WHERE d.source = 'gdrive' AND d.tenant_id = $tenant AND coalesce(d.deleted, false) = false
        RETURN count(d)
        """,
        {"tenant": os.environ.get("WEAVE_TENANT_ID", "demo")},
    ).result_set[0][0]
    print(f"drive_documents={drive_count}")


if __name__ == "__main__":
    main()
