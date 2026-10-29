"""A retry only touches the calling company's failures: another company's token must not fetch them or use up their attempts."""

import pytest

from app.gdrive.worker import retry_drive_failures
from app.github.worker import retry_github_failures
from app.ingest.records import SYNC_FAILURE, MemoryStore
from app.ingest.worker import retry_failures
from app.notion.worker import retry_notion_failures


class Recorder:
    """Every fetch is recorded, then fails, so nothing reaches the graph."""

    def __init__(self):
        self.fetched = []

    def __getattr__(self, name):
        def fetch(*args):
            self.fetched.append(args[-1])
            raise ValueError(name)

        return fetch


SEEDS = {
    "slack": (":slack:failure:C1:{n}", {"channel_id": "C1", "message_ts": "{n}"}),
    "gdrive": (":gdrive:failure:{n}", {"file_id": "{n}", "folder_id": "F1"}),
    "notion": (":notion:failure:{n}", {"page_id": "{n}", "workspace_id": "W1"}),
    "github": (":github:failure:9:issue:{n}", {"number": "{n}", "external_id": "issue:{n}", "kind": "issue", "owner": "o", "repo": "r"}),
}
RETRY = {
    "slack": lambda store, tenant: retry_failures(None, Recorder.current, tenant_id=tenant, store=store),
    "gdrive": lambda store, tenant: retry_drive_failures(None, Recorder.current, store, tenant_id=tenant),
    "notion": lambda store, tenant: retry_notion_failures(None, Recorder.current, store, tenant_id=tenant),
    "github": lambda store, tenant: retry_github_failures(None, Recorder.current, store, tenant_id=tenant),
}


@pytest.mark.parametrize("connector", sorted(SEEDS))
def test_retry_leaves_another_companys_failures_alone(connector):
    suffix, fields = SEEDS[connector]
    store = MemoryStore()
    for tenant, n in (("north", "1111"), ("south", "2222")):
        payload = {key: value.format(n=n) for key, value in fields.items()}
        store.merge(tenant + suffix.format(n=n), tenant, SYNC_FAILURE, {**payload, "tenant_id": tenant, "attempts": 1})
    Recorder.current = Recorder()

    RETRY[connector](store, "north")

    assert Recorder.current.fetched == ["1111"]
    south = store.get("south" + suffix.format(n="2222"))
    assert south["payload"]["attempts"] == 1
