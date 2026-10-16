"""Poll each configured connector once.

Usage: python -m app.poll [--every SECONDS]

A connector whose settings are missing is skipped. One connector failing
does not stop the others. The embedding model loads only when a Postgres
page is indexed, so this process needs the embed extra.
"""

import argparse
import logging
import os
import time
from pathlib import Path

logger = logging.getLogger("weave.poll")


def poll_once(graph, store, env: dict[str, str] | None = None) -> dict[str, str]:
    """Return one status per connector: ok, skip, or the error class."""

    settings = env if env is not None else os.environ
    return {
        "slack": _guard(_slack, graph, store, settings),
        "notion": _guard(_notion, graph, store, settings),
        "gdrive": _guard(_drive, graph, store, settings),
        "github": _guard(_github, graph, store, settings),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--every", type=int, default=0, help="repeat after this many seconds; 0 runs once")
    args = parser.parse_args()
    if args.every < 0:
        parser.error("--every must be 0 or a positive number of seconds")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from shared.env import load_env

    load_env()
    from app.graph.client import connect_graph
    from app.ingest.records import postgres_store

    graph = connect_graph()
    store = postgres_store()
    while True:
        results = poll_once(graph, store)
        print(" ".join(f"{name}={status}" for name, status in results.items()), flush=True)
        if args.every == 0:
            raise SystemExit(0 if _ok(results) else 1)
        if not _ok(results):
            logger.warning("poll finished with a connector error")
        time.sleep(args.every)


def _ok(results: dict[str, str]) -> bool:
    return all(status in {"ok", "skip"} for status in results.values())


def _guard(fn, graph, store, env) -> str:
    try:
        return fn(graph, store, env)
    except Exception as exc:
        logger.warning("%s poll failed: %s", fn.__name__.removeprefix("_"), type(exc).__name__)
        return type(exc).__name__


def _tenant(env) -> str:
    return env.get("WEAVE_TENANT_ID", "").strip() or env.get("WEAVE_TENANT", "").strip()


def _slack(graph, store, env) -> str:
    tenant = _tenant(env)
    connection = env.get("WEAVE_CONNECTION_ID", "").strip()
    workspace = env.get("WEAVE_WORKSPACE_ID", "").strip()
    if not tenant or not connection or not workspace or not env.get("SLACK_BOT_TOKEN", "").strip():
        return "skip"
    from app.ingest.worker import backfill_public_channels, slack_client_from_env

    domain = env.get("SLACK_WORKSPACE_DOMAIN", "").strip() or None
    backfill_public_channels(
        tenant,
        connection,
        workspace,
        slack_client_from_env(),
        graph,
        workspace_domain=domain,
        incremental=True,
        store=store,
    )
    return "ok"


def _notion(graph, store, env) -> str:
    tenant = _tenant(env)
    workspace = env.get("NOTION_WORKSPACE_ID", "").strip()
    if not tenant or not workspace or not env.get("NOTION_TOKEN", "").strip():
        return "skip"
    from app.notion.client import notion_client_from_env
    from app.notion.worker import poll_shared_pages

    poll_shared_pages(
        tenant,
        env.get("NOTION_CONNECTION_ID", "").strip() or "notion-main",
        workspace,
        notion_client_from_env(),
        graph,
        store,
    )
    return "ok"


def _drive(graph, store, env) -> str:
    tenant = _tenant(env)
    folder = env.get("GOOGLE_DRIVE_FOLDER_ID", "").strip()
    credentials = env.get("GOOGLE_SERVICE_ACCOUNT_FILE", "").strip() or "secrets/gdrive-service-account.json"
    if not tenant or not folder or not Path(credentials).is_file():
        return "skip"
    from app.gdrive.client import drive_client_from_env
    from app.gdrive.worker import poll_folder

    poll_folder(
        tenant,
        env.get("GOOGLE_DRIVE_CONNECTION_ID", "").strip() or "gdrive-main",
        folder,
        drive_client_from_env(),
        graph,
        store,
    )
    return "ok"


def _github(graph, store, env) -> str:
    tenant = _tenant(env)
    owner = env.get("GITHUB_OWNER", "").strip()
    repo = env.get("GITHUB_REPO", "").strip()
    if not tenant or not owner or not repo or not env.get("GITHUB_TOKEN", "").strip():
        return "skip"
    from app.github.client import github_client_from_env
    from app.github.worker import poll_repository

    poll_repository(
        tenant,
        env.get("GITHUB_CONNECTION_ID", "").strip() or "github-main",
        owner,
        repo,
        github_client_from_env(),
        graph,
        store,
    )
    return "ok"


if __name__ == "__main__":
    main()
