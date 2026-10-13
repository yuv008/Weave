"""Post, edit, and delete one Slack message and wait for the events route."""

import os
import time
from pathlib import Path

from app.graph.client import connect_graph
from app.ingest.worker import slack_client_from_env

MARKER = "weave-event-check"
EDITED = "weave-event-check-edited"


def load_env() -> None:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def snapshot(graph, node_id: str):
    rows = graph.query(
        """
        MATCH (m:Message {id: $id})
        RETURN m.excerpt, m.deleted
        """,
        {"id": node_id},
    ).result_set
    if not rows:
        return None
    return {"excerpt": rows[0][0] or "", "deleted": bool(rows[0][1])}


def wait_for(graph, node_id: str, predicate, timeout: float = 25.0):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = snapshot(graph, node_id)
        if last is not None and predicate(last):
            return last
        time.sleep(1)
    return last


def main() -> None:
    load_env()
    tenant_id = os.environ["WEAVE_TENANT_ID"]
    client = slack_client_from_env()
    graph = connect_graph()
    channels = client.list_public_channels().get("channels") or []
    channel_id = channels[0]["id"]
    posted = client._call(
        "chat.postMessage",
        {"channel": channel_id, "text": MARKER},
        post=True,
    )
    message_ts = posted["ts"]
    node_id = f"{tenant_id}:slack:message:{channel_id}:{message_ts}"
    print(f"posted_channel={channel_id} posted_ts={message_ts}")

    created = wait_for(graph, node_id, lambda row: row["excerpt"] == MARKER)
    print(f"create_seen={created is not None and created['excerpt'] == MARKER}")

    client._call(
        "chat.update",
        {"channel": channel_id, "ts": message_ts, "text": EDITED},
        post=True,
    )
    edited = wait_for(graph, node_id, lambda row: row["excerpt"] == EDITED)
    print(f"edit_seen={edited is not None and edited['excerpt'] == EDITED}")

    client._call("chat.delete", {"channel": channel_id, "ts": message_ts}, post=True)
    deleted = wait_for(graph, node_id, lambda row: row["deleted"] is True and row["excerpt"] == "")
    print(
        "delete_seen="
        + str(deleted is not None and deleted["deleted"] is True and deleted["excerpt"] == "")
    )


if __name__ == "__main__":
    main()
