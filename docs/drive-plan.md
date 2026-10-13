# Plan: finish Google Drive

**Status:** the demo slice is implemented. The read-only service account can list the shared folder, and one Google Doc is stored in FalkorDB. `requests` is a direct dependency because `google-auth` uses it to refresh the token. Unit tests cover idempotency, tombstones, and the cited answer. No live edit or trash of that Doc has been confirmed.

Ingest Google Docs from one shared folder into FalkorDB as `Document` nodes and answer one cited question from stored excerpts. Slack live delivery and the folder access test have already succeeded.

`BUILD.md` records what already exists.

## Done when

- A poll lists every Google Doc in the configured folder and upserts it into FalkorDB.
- Re-running the poll does not duplicate `Document` nodes.
- The sync watermark moves only after every file in that batch has been written.
- A newer `modifiedTime` updates the excerpt and hash. An older export does not overwrite.
- A trashed or removed file becomes a tombstone: `deleted=true`, excerpt blank, edges kept.
- One phrase over Drive documents for the tenant and folder returns matching excerpts and Drive URLs. Deleted documents are excluded.
- Tests cover idempotency, watermark behavior, tombstones, and the cited answer. No live Google call in unit tests.

## Stay fixed

- Nango is not part of this plan. Use the Google Drive API with a service account JSON key.
- Read-only: service account **weave-drive-reader**, Viewer on one shared folder. No project roles on the service account beyond what is needed to call the API.
- First slice is **Google Docs** (`application/vnd.google-apps.document`) in that folder only. Other MIME types are skipped without download.
- Do not store exported full document text. Keep the hash, the 280-character prefix, and the source URL.
- Do not merge Drive owners into Slack `Person` nodes.
- Do not describe the result as permission-safe. The scope remains `demo_single_tenant`.
- The model provider stays unchosen. The cited answer is assembled from retrieved excerpts only.
- The key file lives at `secrets/gdrive-service-account.json` (or `GOOGLE_SERVICE_ACCOUNT_FILE`). Never log, commit, or copy it into `BUILD.md`.

## Graph model

Node label: `Document`.

Node id: `{tenant}:gdrive:document:{folder_id}:{file_id}`.

Connection id: `{tenant}:gdrive:connection:{connection_id}` (`GOOGLE_DRIVE_CONNECTION_ID`, e.g. `gdrive-main`).

Relationships:

- `(Tenant)-[:HAS_CONNECTION]->(Connection)`
- `(Connection)-[:HAS_SOURCE]->(Document)`

Required properties on `Document`: `id`, `tenant_id`, `source=gdrive`, `connection_id`, `workspace_id` (use folder id or a fixed demo scope id), `external_id` (file id), `object_type=document`, `title`, `version` (RFC3339 `modifiedTime`), `deleted`, `source_url` (`https://drive.google.com/file/d/{file_id}/view`), `excerpt`, `content_hash`, `visibility=demo_single_tenant`, `ingested_at`.

## Phase 0. Access test (no graph write)

Add `scripts/check_drive_access.py`.

Load credentials from `GOOGLE_SERVICE_ACCOUNT_FILE`. Call `files.list` with `q: '{folder_id}' in parents and trashed = false`. Print file id and MIME type only. Do not print file names that contain sensitive content if avoidable; ids and MIME types are enough.

Exit non-zero on `403` or missing folder id.

Run manually after the user shares the folder with the service account email and sets `GOOGLE_DRIVE_FOLDER_ID`.

## Phase 1. Drive API client

Add `app/gdrive/client.py`. Credentials path only through the constructor. Read `GOOGLE_SERVICE_ACCOUNT_FILE` at the process edge.

Methods:

- `list_files(folder_id, page_token)` → `files.list`
- `export_document(file_id)` → `files.export` with `mimeType=text/plain`

Error behavior:

- `429` and `5xx`: retry with backoff, up to 5 attempts.
- `403`, `404`, and other `4xx`: raise typed errors. Do not retry.

Tests use fixtures or mocked HTTP. No live Google call.

## Phase 2. Normalize export to canonical document

Add `app/gdrive/normalize.py`.

Input: a Drive file metadata object plus exported plain text for hashing only.

Rules:

- Only `mimeType == application/vnd.google-apps.document` is ingested.
- `trashed` → tombstone.
- Excerpt is first 280 characters of export. Full export is hashed then dropped.
- `version` is `modifiedTime`. Compare as ISO strings with timezone.

Skip non-Doc files in the list without error.

## Phase 3. Graph upsert

Reuse `app/graph/upsert_document.py` from the Notion plan if it exists, or add Drive-specific upsert with the same stale-version rules as Slack messages.

`write_connection()` with `source=gdrive`. Never store the JSON key on the node.

## Phase 4. Sync state and poll worker

Sync node id: `{tenant}:gdrive:sync:{folder_id}`.

Properties: `tenant_id`, `connection_id`, `folder_id`, `page_token`, `modified_after`, `status`, `last_error_class`, `updated_at`.

Add `app/gdrive/worker.py` with `poll_folder(tenant_id, connection_id, folder_id, client, graph)`.

For each `files.list` page:

1. For each Google Doc, export, normalize, upsert.
2. Advance `page_token` only after every file on that page has been written.
3. When the list has no `nextPageToken`, set `modified_after` to the greatest `modifiedTime` seen.

Incremental poll: use `modifiedTime > watermark` in the query when the API allows, or filter client-side for the demo slice.

Tests: two list pages, crash before checkpoint, replay without duplicates.

## Phase 5. Failed records

`:SyncFailure` id `{tenant}:gdrive:failure:{file_id}`. Same retry cap as Slack. Refetch one file by id and re-export. No full-folder rescan for a single failure.

## Phase 6. Logs

One line per list page: `tenant_id`, `folder_id`, `records`, `failed`, `duration_ms`, `error_class`. No export text or key path in logs.

## Phase 7. One cited answer

Add `app/answer/drive_question.py`.

Input: `tenant_id`, `folder_id`, and a phrase. Match `Document` nodes where `source=gdrive`, `external_id` or properties scope to that folder, `deleted` is false, and `excerpt` contains the phrase. Newest `version` first, limit 20.

No model call.

## Environment

Add these names to `.env.example` when Phase 1 code lands. Values stay empty in git.

- `GOOGLE_SERVICE_ACCOUNT_FILE`
- `GOOGLE_DRIVE_FOLDER_ID`
- `GOOGLE_DRIVE_CONNECTION_ID`
- `WEAVE_TENANT_ID`

## Prerequisites before Phase 1

1. Confirm Slack human `message.channels` delivery (see `BUILD.md`).
2. Create or place the service account key at `secrets/gdrive-service-account.json`.
3. Share one folder with the service account as Viewer.
4. Set `GOOGLE_DRIVE_FOLDER_ID` and pass Phase 0 access test.

## Order

Phase 0 is manual verification. Implement Phases 1–7 in order after prerequisites pass.

After Phase 7, update `BUILD.md` and stop. Cross-source answers (Slack + Notion + Drive) are out of scope for this plan.
