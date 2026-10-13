# Plan: finish Notion

**Status:** the demo slice is implemented. `NOTION_VERSION` is `2026-03-11`. A live poll stored one shared page with a source URL and a `HAS_SOURCE` edge. The excerpt is empty because that page has a title and no supported block text. Unit tests cover idempotency, tombstones, and the cited answer. No live edit or trash of that page has been confirmed.

Ingest shared Notion pages into FalkorDB as `Document` nodes and answer one cited question from stored excerpts. Slack live delivery is already confirmed.

`BUILD.md` records what already exists.

## Done when

- A poll through the integration ingests every page shared with it into FalkorDB.
- Re-running the poll does not duplicate `Document` nodes.
- The sync watermark moves only after every page in that batch has been written.
- An edit with a newer `last_edited_time` updates the excerpt and hash. An older edit does not overwrite.
- A removed or archived page becomes a tombstone: `deleted=true`, excerpt blank, edges kept.
- One phrase over Notion documents for the tenant returns matching excerpts and Notion URLs. Deleted documents are excluded.
- Tests cover idempotency, watermark behavior, tombstones, and the cited answer. No live Notion call in unit tests.

## Stay fixed

- Nango is not part of this plan. Call the Notion REST API directly.
- Do not store the full page body. Keep the hash, the 280-character prefix, and the source URL.
- Do not merge Notion users into Slack `Person` nodes. Do not add `MENTIONS`, Decision extraction, or a web UI in this plan.
- Do not describe the result as permission-safe. The scope remains `demo_single_tenant`.
- The model provider stays unchosen. The cited answer is assembled from retrieved excerpts only.
- Pin `NOTION_VERSION` to `2026-03-11` before Phase 1 ships. That is Notion's current REST version. Do not use `2022-06-28`. On this version, tombstones read `in_trash`. Treat a legacy `archived: true` the same way.

## Graph model

Node label: `Document`.

Node id: `{tenant}:notion:document:{workspace}:{page_id}`.

Connection id: `{tenant}:notion:connection:{connection_id}`.

Relationships:

- `(Tenant)-[:HAS_CONNECTION]->(Connection)`
- `(Connection)-[:HAS_SOURCE]->(Document)`

Required properties on `Document`: `id`, `tenant_id`, `source=notion`, `connection_id`, `workspace_id`, `external_id` (page id), `object_type=document`, `title`, `occurred_at` / `updated_at` from `last_edited_time`, `version` (ISO `last_edited_time` for ordering), `deleted`, `source_url`, `excerpt`, `content_hash`, `visibility=demo_single_tenant`, `ingested_at`.

## Phase 1. Notion REST client

Add `app/notion/client.py`. The token is accepted only through the constructor. The worker reads `NOTION_TOKEN` at the process edge and never logs it.

Send headers on every request:

- `Authorization: Bearer <token>`
- `Notion-Version: <NOTION_VERSION>` once pinned in the environment
- `Content-Type: application/json`

Methods, each returning parsed JSON:

- `users_me()` → `GET /v1/users/me`
- `search_pages(cursor, filter_last_edited_time_after)` → `POST /v1/search` with `filter: { property: object, value: page }` and optional `start_cursor`
- `retrieve_page(page_id)` → `GET /v1/pages/{page_id}`
- `block_children(block_id, cursor)` → `GET /v1/blocks/{block_id}/children`

Error behavior:

- `429`: sleep for `Retry-After`, then retry the same call.
- `5xx` and network failures: exponential backoff with jitter, up to 5 attempts.
- Other `4xx`: raise a typed error. Do not retry.

Tests use recorded JSON fixtures. No live Notion call.

## Phase 2. Normalize page to canonical document

Add `app/models/canonical_document.py` (or extend `app/models/canonical.py` with a `CanonicalDocument` type) and `app/notion/normalize.py`.

Input: a Notion page object plus plain text extracted from blocks for hashing only.

Rules:

- Only `object == page` is accepted. Databases and other types are skipped.
- `in_trash` or `archived` → `deleted=true`, excerpt `""`, hash of empty string.
- Otherwise excerpt is the first 280 characters of extracted plain text. Full text is used only to compute `content_hash`, then dropped.
- `source_url` is the Notion page URL from the API when present, or `https://www.notion.so/{page_id_without_dashes}` as a fallback.
- `version` is the page's `last_edited_time` string. Lexicographic compare must match chronological order (ISO-8601 with timezone).

Malformed payloads raise `ValueError` for the failure list. They do not advance the watermark.

## Phase 3. Extract plain text from blocks

Add `app/notion/blocks.py`.

Walk `block_children` for the page id with pagination. Collect plain text from supported block types (`paragraph`, `heading_1`, `heading_2`, `heading_3`, `bulleted_list_item`, `numbered_list_item`, `to_do`, `quote`, `callout`). Skip unsupported types without failing the page.

Do not store block trees in the graph. Do not follow child databases in this slice.

## Phase 4. Graph upsert

Add `app/graph/upsert_document.py`.

One parameterized transaction per document:

- `MERGE` `Document` by `id`.
- Stale-write guard: if stored `version` is newer than the incoming `last_edited_time`, no-op property writes. Equal version re-merges edges only.
- `MERGE` `Connection` and `(Connection)-[:HAS_SOURCE]->(Document)`.
- Excerpt and title travel only as query parameters, never in the Cypher string.

Add indexes for `:Document(id)` and `:Document(tenant_id)` in `ensure_indexes()`.

Tests: idempotent upsert, older version rejected, equal version edge repair, tombstone keeps `HAS_SOURCE`.

## Phase 5. Sync state and poll worker

Reuse the `:SyncState` pattern from Slack with Notion-specific ids.

Sync node id: `{tenant}:notion:sync:{workspace}`.

Properties: `tenant_id`, `connection_id`, `workspace_id`, `last_edited_after`, `search_cursor`, `status`, `last_error_class`, `updated_at`.

Add `app/notion/worker.py` with `poll_shared_pages(tenant_id, connection_id, workspace_id, client, graph)`.

For each search page:

1. For each result page id, retrieve the page, extract blocks, normalize, upsert.
2. Write the checkpoint only after every page on that search page has been written.
3. When `search` returns an empty `next_cursor`, set `last_edited_after` to the greatest `last_edited_time` seen and mark `complete` for that run.

Incremental poll: pass `filter_last_edited_time_after` from the stored watermark.

Also call `write_connection()` with `source=notion` on first run. Do not store `NOTION_TOKEN` on the node.

Tests with a fake client and FalkorDB: two search pages, crash before checkpoint, replay without duplicates.

## Phase 6. Failed records

Reuse or mirror `:SyncFailure` with id `{tenant}:notion:failure:{page_id}`.

Properties: `node_id`, `version`, `error_class`, `attempts`, `updated_at`. No excerpt, no body, no token.

`retry_notion_failures(graph, client)` retrieves one page by id and upserts. Fifth failure stays on the list.

## Phase 7. Logs

One structured line per search page: `tenant_id`, `workspace_id`, `records`, `failed`, `duration_ms`, `error_class`. No page text or token in logs.

## Phase 8. One cited answer

Add `app/answer/notion_question.py`.

Input: `tenant_id` and a phrase. Match `Document` nodes where `source=notion`, `tenant_id` matches, `deleted` is false, and `excerpt` contains the phrase. Return excerpt, `updated_at`, and `source_url`, newest first, limit 20.

If there are no hits, say the graph has no matching Notion document. No model call.

Test: one deleted document excluded; live hit includes URL.

## Environment

Add these names to `.env.example` when Phase 1 code lands. Values stay empty in git.

- `NOTION_TOKEN`
- `NOTION_VERSION`
- `NOTION_WORKSPACE_ID`
- `NOTION_CONNECTION_ID`
- `WEAVE_TENANT_ID` (reuse demo tenant)

## Prerequisites before Phase 1

1. Confirm Slack human `message.channels` delivery (see `BUILD.md`).
2. Share at least one page with the internal integration in Notion.
3. Pin `NOTION_VERSION` from Notion's API documentation.

## Order

Implement phases in order. Phase 8 starts only after Phase 4 can write a tombstone the answer query excludes.

After Phase 8, update `BUILD.md` and stop. Google Drive is a separate plan.
