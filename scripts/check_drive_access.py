"""List file ids and MIME types in one shared Drive folder. No graph writes."""

import os
import sys
from pathlib import Path

from app.gdrive.client import DriveRequestError, drive_client_from_env


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
    folder_id = os.environ.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    if not folder_id:
        print("GOOGLE_DRIVE_FOLDER_ID is empty")
        return 1
    client = drive_client_from_env()
    token = None
    try:
        while True:
            page = client.list_files(folder_id, token)
            for item in page.get("files") or []:
                if isinstance(item, dict):
                    print(f"{item.get('id')} {item.get('mimeType')}")
            token = page.get("nextPageToken") or ""
            if not token:
                return 0
    except DriveRequestError as exc:
        print(f"drive_error status={exc.status} error={exc.error}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
