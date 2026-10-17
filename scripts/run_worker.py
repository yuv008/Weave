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
    return _set("SLACK_BOT_TOKEN", "WEAVE_TENANT_ID", "WEAVE_CONNECTION_ID", "WEAVE_WORKSPACE_ID")


def notion_ready() -> bool:
    return _set("NOTION_TOKEN", "NOTION_WORKSPACE_ID")


def drive_ready() -> bool:
    return _set("GOOGLE_DRIVE_FOLDER_ID")


def github_ready() -> bool:
    return _set("GITHUB_TOKEN", "GITHUB_OWNER", "GITHUB_REPO")


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


def _slack_once() -> None:
    from app.graph.client import connect_graph
    from app.ingest.worker import backfill_public_channels, retry_failures, slack_client_from_env

    client = slack_client_from_env()
    graph = connect_graph()
    domain = _env("SLACK_WORKSPACE_DOMAIN") or None
    # incremental keeps channels that already finished their backfill.
    backfill_public_channels(
        _env("WEAVE_TENANT_ID"),
        _env("WEAVE_CONNECTION_ID"),
        _env("WEAVE_WORKSPACE_ID"),
        client,
        graph,
        workspace_domain=domain,
        incremental=True,
    )
    retry_failures(graph, client, workspace_domain=domain)


def _notion_once() -> None:
    from app.graph.client import connect_graph
    from app.notion.client import notion_client_from_env
    from app.notion.worker import poll_shared_pages, retry_notion_failures

    client = notion_client_from_env()
    graph = connect_graph()
    poll_shared_pages(
        _env("WEAVE_TENANT_ID", "demo"),
        _env("NOTION_CONNECTION_ID", "notion-main"),
        _env("NOTION_WORKSPACE_ID"),
        client,
        graph,
    )
    retry_notion_failures(graph, client)


def _drive_once() -> None:
    from app.gdrive.client import drive_client_from_env
    from app.gdrive.worker import poll_folder, retry_drive_failures
    from app.graph.client import connect_graph

    client = drive_client_from_env()
    graph = connect_graph()
    poll_folder(
        _env("WEAVE_TENANT_ID", "demo"),
        _env("GOOGLE_DRIVE_CONNECTION_ID", "gdrive-main"),
        _env("GOOGLE_DRIVE_FOLDER_ID"),
        client,
        graph,
    )
    retry_drive_failures(graph, client)


def _github_once() -> None:
    from app.github.client import github_client_from_env
    from app.github.worker import poll_repository, retry_github_failures
    from app.graph.client import connect_graph

    client = github_client_from_env()
    graph = connect_graph()
    poll_repository(
        _env("WEAVE_TENANT_ID", "demo"),
        _env("GITHUB_CONNECTION_ID", "github-main"),
        _env("GITHUB_OWNER"),
        _env("GITHUB_REPO"),
        client,
        graph,
    )
    retry_github_failures(graph, client)


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
        try:
            once()
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
        if not ready():
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
