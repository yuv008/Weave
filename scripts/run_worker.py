"""Run the impact watcher and the connector sync loops.

Usage: python scripts/run_worker.py

The watcher is a child process: ``python -m agents.watcher --every``.
Slack, Notion, Drive, and GitHub each poll on their own thread, and only
when the env vars that connector needs are set. A failure is logged and
does not stop the other loops. Webhooks stay on ``app.main`` and are not
started here.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

log = logging.getLogger("weave.worker")
_stop = threading.Event()
_watcher: subprocess.Popen | None = None
_watcher_lock = threading.Lock()


def _set(*names: str) -> bool:
    return all(os.environ.get(name, "").strip() for name in names)


def slack_ready() -> bool:
    from app.connect.credentials import slack_bot_token, slack_workspace_id

    tenant = _env("WEAVE_TENANT_ID") or _env("WEAVE_TENANT")
    return bool(tenant and slack_bot_token() and slack_workspace_id())


def notion_ready() -> bool:
    from app.connect.credentials import notion_token, notion_workspace_id

    tenant = _env("WEAVE_TENANT_ID") or _env("WEAVE_TENANT")
    return bool(tenant and notion_token() and notion_workspace_id())


def drive_ready() -> bool:
    from app.connect.credentials import google_folder_id

    return bool(google_folder_id())


def github_ready() -> bool:
    from app.connect.credentials import github_access_token, github_coordinates

    owner, repo = github_coordinates()
    return bool(owner and repo and github_access_token())


def _grant_tenants(name: str) -> list[str]:
    provider = "google" if name == "drive" else name
    try:
        from app.connect.credentials import current_store

        grants = current_store().list_grants()
    except Exception:
        log.exception("could not list connector grants")
        return []
    tenants = []
    for grant in grants:
        tenant = str(grant.get("tenant_id") or "")
        if grant.get("provider") != provider or grant.get("status") == "failed":
            continue
        if not tenant or tenant == "acme" or not _grant_ready(name, tenant):
            continue
        tenants.append(tenant)
    return tenants


def _grant_ready(name: str, tenant: str) -> bool:
    from app.connect.credentials import (
        github_access_token,
        github_coordinates,
        google_access_token,
        google_folder_id,
        notion_token,
        notion_workspace_id,
        slack_bot_token,
        slack_workspace_id,
    )

    if name == "slack":
        return bool(slack_bot_token(tenant) and slack_workspace_id(tenant))
    if name == "notion":
        return bool(notion_token(tenant) and notion_workspace_id(tenant))
    if name == "drive":
        return bool(google_folder_id(tenant) and google_access_token(tenant))
    if name == "github":
        owner, repo = github_coordinates(tenant)
        return bool(owner and repo and github_access_token(tenant))
    return False


def _mark_connected(name: str, tenant: str) -> None:
    provider = "google" if name == "drive" else name
    try:
        from app.connect.credentials import current_store

        current_store().update_status(tenant, provider, "connected")
    except Exception:
        log.warning("grant status was not updated")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _slack_once(tenant: str | None = None) -> bool:
    from app.graph.client import connect_graph
    from app.ingest.worker import backfill_public_channels, retry_failures
    from app.slack.client import SlackClient

    from app.connect.credentials import slack_bot_token, slack_workspace_id

    chosen = tenant or _env("WEAVE_TENANT_ID") or _env("WEAVE_TENANT")
    token = slack_bot_token(tenant) if tenant else slack_bot_token()
    workspace = slack_workspace_id(tenant) if tenant else slack_workspace_id()
    if not chosen or not token or not workspace:
        return False
    client = SlackClient(token)
    graph = connect_graph(tenant=chosen)
    domain = _env("SLACK_WORKSPACE_DOMAIN") or None
    # incremental keeps channels that already finished their backfill.
    backfill_public_channels(
        chosen,
        _env("WEAVE_CONNECTION_ID", "slack-main"),
        workspace,
        client,
        graph,
        workspace_domain=domain,
        incremental=True,
    )
    retry_failures(graph, client, workspace_domain=domain)
    return True


def _notion_once(tenant: str | None = None) -> bool:
    from app.graph.client import connect_graph
    from app.notion.client import PINNED_NOTION_VERSION, NotionClient
    from app.notion.worker import poll_shared_pages, retry_notion_failures

    from app.connect.credentials import notion_token, notion_workspace_id

    chosen = tenant or _env("WEAVE_TENANT_ID") or _env("WEAVE_TENANT") or "demo"
    token = notion_token(tenant) if tenant else notion_token()
    workspace = notion_workspace_id(tenant) if tenant else notion_workspace_id()
    if not token or not workspace:
        return False
    version = _env("NOTION_VERSION") or PINNED_NOTION_VERSION
    client = NotionClient(token, version=version)
    graph = connect_graph(tenant=chosen)
    poll_shared_pages(
        chosen,
        _env("NOTION_CONNECTION_ID", "notion-main"),
        workspace,
        client,
        graph,
    )
    retry_notion_failures(graph, client)
    return True


def _drive_once(tenant: str | None = None) -> bool:
    from app.gdrive.client import DriveClient, drive_client_from_env
    from app.gdrive.worker import poll_folder, retry_drive_failures
    from app.graph.client import connect_graph

    from app.connect.credentials import google_access_token, google_folder_id

    chosen = tenant or _env("WEAVE_TENANT_ID") or _env("WEAVE_TENANT") or "demo"
    folder = google_folder_id(tenant) if tenant else google_folder_id()
    if tenant:
        token = google_access_token(tenant)
        if not token or not folder:
            return False
        client = DriveClient(token=token)
    else:
        if not folder:
            return False
        client = drive_client_from_env()
    graph = connect_graph(tenant=chosen)
    poll_folder(
        chosen,
        _env("GOOGLE_DRIVE_CONNECTION_ID", "gdrive-main"),
        folder,
        client,
        graph,
    )
    retry_drive_failures(graph, client)
    return True


def _github_once(tenant: str | None = None) -> bool:
    from app.github.client import PINNED_GITHUB_API_VERSION, GitHubClient
    from app.github.worker import poll_repository, retry_github_failures
    from app.graph.client import connect_graph

    from app.connect.credentials import github_access_token, github_coordinates

    chosen = tenant or _env("WEAVE_TENANT_ID") or _env("WEAVE_TENANT") or "demo"
    owner, repo = github_coordinates(tenant) if tenant else github_coordinates()
    token = github_access_token(tenant) if tenant else github_access_token()
    if not owner or not repo or not token:
        return False
    version = _env("GITHUB_API_VERSION") or PINNED_GITHUB_API_VERSION
    client = GitHubClient(token, api_version=version)
    graph = connect_graph(tenant=chosen)
    poll_repository(
        chosen,
        _env("GITHUB_CONNECTION_ID", "github-main"),
        owner,
        repo,
        client,
        graph,
    )
    retry_github_failures(graph, client)
    return True


def _every() -> int:
    try:
        from shared.config import load_config

        return load_config().watcher.every
    except Exception:
        log.exception("could not read watcher.every; using 60s")
        return 60


def _request_stop(signum, _frame) -> None:
    log.info("stopping (signal %s)", signum)
    _stop.set()


def _terminate(proc: subprocess.Popen) -> None:
    if proc.poll() is not None:
        return
    proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=5)


def _supervise_process(name: str, args: list[str], every: int) -> None:
    global _watcher
    while not _stop.is_set():
        proc = None
        try:
            log.info("starting %s", name)
            proc = subprocess.Popen(args, cwd=ROOT)
            with _watcher_lock:
                _watcher = proc
            while proc.poll() is None and not _stop.is_set():
                try:
                    proc.wait(timeout=1)
                except subprocess.TimeoutExpired:
                    continue
        except Exception:
            log.exception("%s failed", name)
        if _stop.is_set():
            if proc is not None:
                _terminate(proc)
            return
        if proc is not None and proc.poll() is not None:
            log.error("%s exited code=%s", name, proc.returncode)
        if _stop.wait(every):
            if proc is not None:
                _terminate(proc)
            return


def _poll_loop(name: str, once, every: int) -> None:
    while not _stop.is_set():
        tenants = _grant_tenants(name) or [None]
        for tenant in tenants:
            try:
                if once(tenant) and tenant:
                    _mark_connected(name, tenant)
            except Exception:
                log.exception("%s sync failed", name)
        if _stop.wait(every):
            return


def _connectors():
    return (
        ("slack", slack_ready, _slack_once),
        ("notion", notion_ready, _notion_once),
        ("drive", drive_ready, _drive_once),
        ("github", github_ready, _github_once),
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    from shared.env import load_env

    load_env()
    try:
        from shared.pg import warm_pg

        warm_pg()
    except Exception:
        log.exception("postgres pool was not warmed")
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            signal.signal(sig, _request_stop)
        except (ValueError, OSError):
            continue
    every = _every()
    threading.Thread(
        target=_supervise_process,
        name="watcher",
        args=("watcher", [sys.executable, "-m", "agents.watcher", "--every"], every),
        daemon=True,
    ).start()
    for name, ready, once in _connectors():
        if not ready() and not _grant_tenants(name):
            log.info("%s sync skipped", name)
            continue
        log.info("%s sync started", name)
        threading.Thread(target=_poll_loop, args=(name, once, every), name=name, daemon=True).start()
    while not _stop.is_set():
        _stop.wait(3600)
    with _watcher_lock:
        proc = _watcher
    if proc is not None:
        _terminate(proc)


if __name__ == "__main__":
    main()
