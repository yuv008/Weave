"""Edit or delete one known Slack message and wait for the events route.

Prints lengths and flags only. Does not print the excerpt.
"""

import os
import time
from pathlib import Path

from app.graph.client import connect_graph
from app.ingest.worker import slack_client_from_env
from app.slack.client import SlackRequestError

EDITED = "weave-live-edit"


def load_env() -> None:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def target() -> tuple[str, str, str]:
    channel = os.environ.get("SLACK_CHANNEL_ID", "").strip()
    ts = os.environ.get("SLACK_MESSAGE_TS", "").strip()
    tenant = os.environ.get("WEAVE_TENANT_ID", "").strip()
    if not channel or not ts or not tenant:
        raise SystemExit("Set SLACK_CHANNEL_ID, SLACK_MESSAGE_TS, and WEAVE_TENANT_ID in .env")
    return channel, ts, f"{tenant}:slack:message:{channel}:{ts}"


def snapshot(graph, node_id: str) -> tuple | None:
    rows = graph.query(
        """
        MATCH (c:Conversation)-[edge:CONTAINS]->(m:Message {id: $id})
        RETURN size(coalesce(m.excerpt, '')), m.deleted, m.version, count(edge)
        """,
        {"id": node_id},
    ).result_set
    return rows[0] if rows else None


def wait_for(graph, node_id: str, predicate, timeout: float = 30.0):
    deadline = time.time() + timeout
    last = snapshot(graph, node_id)
    while time.time() < deadline:
        last = snapshot(graph, node_id)
        if last is not None and predicate(last):
            return last
        time.sleep(1)
    return last


def main() -> None:
    load_env()
    channel, ts, node_id = target()
    graph = connect_graph()
    before = snapshot(graph, node_id)
    print(f"before_len={before[0] if before else 'missing'} before_deleted={before[1] if before else ''} before_contains={before[3] if before else 0}")
    client = slack_client_from_env()
    try:
        client._call(
            "chat.update",
            {"channel": channel, "ts": ts, "text": EDITED},
            post=True,
        )
        print("slack_action=update")
        after = wait_for(
            graph,
            node_id,
            lambda row: row[0] == len(EDITED) and row[1] is False,
        )
    except SlackRequestError as exc:
        print(f"update_error={exc.error}")
        client._call("chat.delete", {"channel": channel, "ts": ts}, post=True)
        print("slack_action=delete")
        after = wait_for(graph, node_id, lambda row: row[1] is True and row[0] == 0)
    if after is None:
        print("after=missing")
        return
    print(
        f"after_len={after[0]} after_deleted={after[1]} "
        f"after_version={after[2]} after_contains={after[3]}"
    )


if __name__ == "__main__":
    main()
