import hashlib
import json
from datetime import UTC, datetime

import pytest

from app.answer.drive_question import answer_drive_question
from app.gdrive.client import DOC_MIME, DriveClient, DriveRequestError
from app.gdrive.normalize import normalize_drive_file
from app.gdrive.worker import drive_sync_id, poll_folder
from app.ingest.records import MemoryStore
from app.graph.upsert_document import upsert_document
from app.models.canonical_document import CanonicalDocument

TOKEN = "drive-test-token"


class Transport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, url, headers):
        self.calls.append((url, headers))
        status, body, header = self.responses.pop(0)
        if isinstance(body, Exception):
            raise body
        payload = body if isinstance(body, bytes) else json.dumps(body).encode()
        return status, header, payload


def _client(responses):
    transport = Transport(responses)
    sleeps = []
    client = DriveClient(token=TOKEN, transport=transport, sleep=sleeps.append, random_fn=lambda: 0.0)
    return client, transport, sleeps


def test_drive_list_retries_server_errors_and_keeps_the_token_out_of_the_url():
    client, transport, sleeps = _client(
        [
            (503, b"unavailable", {}),
            (200, {"files": [{"id": "f1", "mimeType": DOC_MIME}]}, {}),
        ]
    )
    page = client.list_files("folder1")
    assert page["files"][0]["id"] == "f1"
    assert len(sleeps) == 1
    url, headers = transport.calls[0]
    assert TOKEN not in url
    assert "folder1" in url
    assert headers["Authorization"] == f"Bearer {TOKEN}"


def test_drive_403_is_not_retried():
    client, transport, sleeps = _client([(403, {"error": {"status": "PERMISSION_DENIED"}}, {})])
    with pytest.raises(DriveRequestError) as caught:
        client.list_files("folder1")
    assert caught.value.status == 403
    assert sleeps == []
    assert len(transport.calls) == 1


def test_non_docs_are_skipped_and_trash_drops_the_body():
    assert (
        normalize_drive_file(
            {"id": "f1", "mimeType": "application/pdf", "modifiedTime": "2026-03-11T00:00:00.000Z"},
            "pdf",
            tenant_id="demo",
            connection_id="gdrive-main",
            folder_id="folder1",
        )
        is None
    )
    secret = "full doc body"
    document = normalize_drive_file(
        {
            "id": "doc1",
            "mimeType": DOC_MIME,
            "modifiedTime": "2026-03-11T00:00:00.000Z",
            "trashed": True,
            "name": "Secret",
        },
        secret,
        tenant_id="demo",
        connection_id="gdrive-main",
        folder_id="folder1",
    )
    assert document is not None
    assert document.deleted is True
    assert document.excerpt == ""
    assert secret not in document.model_dump_json()
    assert document.node_id == "demo:gdrive:document:folder1:doc1"
    assert document.source_url == "https://drive.google.com/file/d/doc1/view"


def _doc(**overrides) -> CanonicalDocument:
    fields = {
        "tenant_id": "demo",
        "connection_id": "gdrive-main",
        "source": "gdrive",
        "workspace_id": "folder1",
        "external_id": "doc1",
        "title": "Plan",
        "occurred_at": datetime(2026, 3, 11, tzinfo=UTC),
        "updated_at": datetime(2026, 3, 11, tzinfo=UTC),
        "version": "2026-03-11T00:00:00.000000Z",
        "excerpt": "annual plan",
        "source_url": "https://drive.google.com/file/d/doc1/view",
        "content_hash": hashlib.sha256(b"annual plan").hexdigest(),
    }
    fields.update(overrides)
    return CanonicalDocument(**fields)


class _Folder:
    def __init__(self):
        self.stop = True
        self.calls = []

    def list_files(self, folder_id, page_token=None, modified_after=None):
        self.calls.append(page_token)
        if page_token == "page-2":
            if self.stop:
                raise RuntimeError("stop after page 1")
            return {"files": [_file("doc2", "2026-03-10T00:00:00.000Z")]}
        return {"files": [_file("doc1", "2026-03-11T00:00:00.000Z"), {"id": "pdf", "mimeType": "application/pdf"}], "nextPageToken": "page-2"}

    def export_document(self, file_id):
        return f"text {file_id}"


def _file(file_id, modified):
    return {"id": file_id, "mimeType": DOC_MIME, "modifiedTime": modified, "name": file_id, "trashed": False}


def test_drive_checkpoint_survives_a_crash_and_replay_does_not_duplicate(graph):
    client = _Folder()
    store = MemoryStore()
    with pytest.raises(RuntimeError, match="stop after page 1"):
        poll_folder("demo", "gdrive-main", "folder1", client, graph, store)
    payload = store.get(drive_sync_id("demo", "folder1"))["payload"]
    assert payload.get("page_token") == "page-2"
    assert payload.get("status") == "in_progress"
    client.stop = False
    poll_folder("demo", "gdrive-main", "folder1", client, graph, store)
    count = graph.query(
        "MATCH (d:Document) WHERE d.source = 'gdrive' AND d.tenant_id = 'demo' RETURN count(d)"
    ).result_set[0][0]
    assert count == 0
    poll_folder("demo", "gdrive-main", "folder1", client, graph, store)
    assert (
        graph.query(
            "MATCH (d:Document) WHERE d.source = 'gdrive' AND d.tenant_id = 'demo' RETURN count(d)"
        ).result_set[0][0]
        == 0
    )


def test_unknown_drive_folder_is_not_written(graph):
    assert upsert_document(graph, _doc()).applied is False
    answer = answer_drive_question(graph, "demo", "folder1", "annual plan")
    assert answer.citations == ()
