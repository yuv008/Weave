"""Check local Slack setup. Prints no tokens or signing secrets."""

import os
from pathlib import Path

from app.graph.client import connect_graph
from app.ingest.worker import backfill_public_channels, slack_client_from_env
from app.slack.client import SlackRequestError


def load_env(path: Path) -> None:
    for line in path.read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        os.environ[key.strip()] = value.strip().strip('"').strip("'")


def main() -> None:
    env_path = Path(".env")
    load_env(env_path)
    token = os.environ.get("SLACK_BOT_TOKEN", "")
    secret = os.environ.get("SLACK_SIGNING_SECRET", "")
    print(f"SLACK_BOT_TOKEN set={bool(token)} xoxb={token.startswith('xoxb-')} length={len(token)}")
    print(f"SLACK_SIGNING_SECRET set={bool(secret)} length={len(secret)}")
    for key in (
        "FALKORDB_HOST",
        "FALKORDB_PORT",
        "FALKORDB_GRAPH",
        "SLACK_WORKSPACE_DOMAIN",
        "WEAVE_TENANT_ID",
        "WEAVE_CONNECTION_ID",
        "WEAVE_WORKSPACE_ID",
    ):
        print(f"{key}={os.environ.get(key, '')}")

    client = slack_client_from_env()
    try:
        identity = client._call("auth.test", {})
    except SlackRequestError as exc:
        print(f"auth.test failed status={exc.status} error={exc.error}")
        raise SystemExit(1) from exc
    print(
        "auth.test ok={ok} team_id={team} team={name} url={url}".format(
            ok=identity.get("ok"),
            team=identity.get("team_id"),
            name=identity.get("team"),
            url=identity.get("url"),
        )
    )
    expected = os.environ.get("WEAVE_WORKSPACE_ID", "")
    if expected and identity.get("team_id") != expected:
        print("workspace id does not match auth.test team_id")
        raise SystemExit(1)

    page = client.list_public_channels()
    channels = page.get("channels") or []
    print(f"public_channels_on_first_page={len(channels)}")

    graph = connect_graph()
    graph.query("RETURN 1")
    backfill_public_channels(
        os.environ["WEAVE_TENANT_ID"],
        os.environ["WEAVE_CONNECTION_ID"],
        os.environ["WEAVE_WORKSPACE_ID"],
        client,
        graph,
        workspace_domain=os.environ.get("SLACK_WORKSPACE_DOMAIN") or None,
    )
    count = graph.query(
        "MATCH (m:Message) WHERE m.tenant_id = $tenant RETURN count(m)",
        {"tenant": os.environ["WEAVE_TENANT_ID"]},
    ).result_set[0][0]
    channels_written = graph.query(
        "MATCH (c:Conversation) WHERE c.tenant_id = $tenant RETURN count(c)",
        {"tenant": os.environ["WEAVE_TENANT_ID"]},
    ).result_set[0][0]
    print(f"graph_messages={count} graph_conversations={channels_written}")


if __name__ == "__main__":
    main()
