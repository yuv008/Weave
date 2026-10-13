"""One live pass over the connected public Slack workspace.

Prints counts and citation URLs only. Does not print tokens or message bodies
other than the marker phrase this script posts.
"""

import hashlib
import hmac
import json
import os
import time
from pathlib import Path

from fastapi.testclient import TestClient

from app.answer.channel_question import answer_channel_question
from app.graph.client import connect_graph
from app.ingest.worker import backfill_public_channels, slack_client_from_env
from app.main import create_app

MARKER = "weave-e2e-marker"
EDITED = "weave-e2e-marker-edited"


def load_env() -> None:
    for line in Path(".env").read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def count_messages(graph, tenant_id: str) -> int:
    return graph.query(
        "MATCH (m:Message) WHERE m.tenant_id = $tenant RETURN count(m)",
        {"tenant": tenant_id},
    ).result_set[0][0]


def sign(secret: str, body: bytes, timestamp: str) -> str:
    digest = hmac.new(
        secret.encode(),
        f"v0:{timestamp}:".encode() + body,
        hashlib.sha256,
    ).hexdigest()
    return f"v0={digest}"


def post_event(client: TestClient, secret: str, payload: dict) -> int:
    body = json.dumps(payload).encode()
    timestamp = str(int(time.time()))
    response = client.post(
        "/slack/events",
        content=body,
        headers={
            "content-type": "application/json",
            "x-slack-request-timestamp": timestamp,
            "x-slack-signature": sign(secret, body, timestamp),
        },
    )
    return response.status_code


def main() -> None:
    load_env()
    tenant_id = os.environ["WEAVE_TENANT_ID"]
    connection_id = os.environ["WEAVE_CONNECTION_ID"]
    workspace_id = os.environ["WEAVE_WORKSPACE_ID"]
    domain = os.environ.get("SLACK_WORKSPACE_DOMAIN") or None
    secret = os.environ["SLACK_SIGNING_SECRET"]
    client = slack_client_from_env()
    graph = connect_graph()

    before = count_messages(graph, tenant_id)
    backfill_public_channels(
        tenant_id,
        connection_id,
        workspace_id,
        client,
        graph,
        workspace_domain=domain,
    )
    after_replay = count_messages(graph, tenant_id)
    print(f"replay_before={before} replay_after={after_replay}")

    channels = client.list_public_channels().get("channels") or []
    channel_id = channels[0]["id"]
    posted = client._call(
        "chat.postMessage",
        {"channel": channel_id, "text": MARKER},
        post=True,
    )
    message_ts = posted["ts"]
    print(f"posted_channel={channel_id} posted_ts={message_ts}")

    backfill_public_channels(
        tenant_id,
        connection_id,
        workspace_id,
        client,
        graph,
        workspace_domain=domain,
        incremental=True,
    )
    after_incremental = count_messages(graph, tenant_id)
    answer = answer_channel_question(graph, tenant_id, channel_id, MARKER)
    print(f"after_incremental={after_incremental} citations={len(answer.citations)}")
    for citation in answer.citations:
        print(f"citation_url={citation.source_url}")
        print(f"citation_matches_marker={MARKER in citation.excerpt}")

    edited_ts = f"{time.time():.6f}"
    client._call(
        "chat.update",
        {"channel": channel_id, "ts": message_ts, "text": EDITED},
        post=True,
    )
    http = TestClient(
        create_app(
            graph=graph,
            signing_secret=secret,
            tenant_id=tenant_id,
            connection_id=connection_id,
            workspace_id=workspace_id,
            workspace_domain=domain,
        )
    )
    edit_status = post_event(
        http,
        secret,
        {
            "type": "event_callback",
            "event_id": f"Ev-edit-{message_ts}",
            "event": {
                "type": "message",
                "subtype": "message_changed",
                "channel": channel_id,
                "channel_type": "channel",
                "message": {
                    "text": EDITED,
                    "ts": message_ts,
                    "edited": {"ts": edited_ts},
                },
            },
        },
    )
    edited_answer = answer_channel_question(graph, tenant_id, channel_id, EDITED)
    stale_answer = answer_channel_question(graph, tenant_id, channel_id, MARKER)
    print(
        f"edit_status={edit_status} edited_citations={len(edited_answer.citations)} "
        f"stale_marker_citations={len(stale_answer.citations)}"
    )

    delete_ts = f"{time.time():.6f}"
    client._call(
        "chat.delete",
        {"channel": channel_id, "ts": message_ts},
        post=True,
    )
    delete_status = post_event(
        http,
        secret,
        {
            "type": "event_callback",
            "event_id": f"Ev-delete-{message_ts}",
            "event": {
                "type": "message",
                "subtype": "message_deleted",
                "channel": channel_id,
                "channel_type": "channel",
                "ts": delete_ts,
                "deleted_ts": message_ts,
            },
        },
    )
    node_id = f"{tenant_id}:slack:message:{channel_id}:{message_ts}"
    excerpt, deleted, contains = graph.query(
        """
        MATCH (c:Conversation)-[edge:CONTAINS]->(m:Message {id: $id})
        RETURN m.excerpt, m.deleted, count(edge)
        """,
        {"id": node_id},
    ).result_set[0]
    gone = answer_channel_question(graph, tenant_id, channel_id, EDITED)
    print(
        f"delete_status={delete_status} excerpt_blank={excerpt == ''} "
        f"deleted={deleted} contains={contains} deleted_citations={len(gone.citations)}"
    )


if __name__ == "__main__":
    main()
