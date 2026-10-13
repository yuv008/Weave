# Plan: finish Slack

**Status:** the demo slice is implemented. Live checks on the connected workspace covered a public backfill, a second run that did not duplicate nodes, a cited archive URL, a human `message.channels` delivery, and a delete that blanked the excerpt and kept `CONTAINS`. The workspace id stays in `.env`. OAuth, private channels, group DMs, and DMs are still out of scope. The Cloudflare events tunnel is temporary.

Finish one public Slack workspace from fetch through a cited answer. Do not start a second connector in this plan.

`BUILD.md` records what already exists. This plan starts from that baseline.

## Done when

- A bot token in the environment can list public channels and backfill their messages into FalkorDB.
- Re-running the backfill does not duplicate nodes.
- The channel checkpoint moves only after that page, including its thread replies, has been written.
- A failed message can be retried without re-reading the whole workspace.
- `message_changed` and `message_deleted` update the same node. Deletes blank the excerpt and keep the edges.
- Private channels, group DMs, and DMs are never requested.
- Logs contain tenant, channel, counts, duration, and error class. They do not contain message text or tokens.
- One question about a public channel returns the matching excerpts and their Slack URLs.
- Tests cover the checkpoint rule, idempotency, tombstones, a rejected webhook signature, and the cited answer.

## Stay fixed

- Nango is not part of this plan. Call Slack's Web API and Events API directly.
- Do not store the full message body. Keep the hash, the 280-character prefix, and the source URL.
- Do not add Decision, `MENTIONS`, person profiles, files, reactions, or a web UI.
- Do not describe the result as permission-safe. The scope remains `demo_single_tenant`.
- The model provider stays unchosen. The cited answer in the last phase is assembled from retrieved excerpts.

## Phase 1. Slack Web API client

Add `app/slack/client.py`. It accepts a token only through its constructor. The worker reads `SLACK_BOT_TOKEN` from the environment at the process edge and never logs it.

Methods, each returning the parsed JSON body:

- `list_public_channels(cursor)` calls `conversations.list` with `types=public_channel` and `limit=200`.
- `history(channel_id, cursor, oldest)` calls `conversations.history`.
- `replies(channel_id, parent_ts, cursor)` calls `conversations.replies`.

Error behavior:

- `429`: sleep for the `Retry-After` value, then retry the same call.
- `5xx` and network failures: exponential backoff with jitter, up to 5 attempts.
- `channel_not_found`, `not_in_channel`, and other `4xx` responses: raise a typed error the worker can record. Do not retry those.

Tests use recorded JSON fixtures. No live Slack call.

## Phase 2. Sync state

Store checkpoints in FalkorDB as `:SyncState` nodes. This is temporary until a relational database is chosen.

Node id: `{tenant}:slack:sync:{workspace}:{channel}`.

Properties: `tenant_id`, `connection_id`, `workspace_id`, `channel_id`, `history_cursor`, `oldest`, `status`, `last_error_class`, `updated_at`.

Thread pages use a child id `{tenant}:slack:sync:{workspace}:{channel}:{parent_ts}` with `replies_cursor`.

Write the checkpoint in a separate query only after `upsert_message()` has returned for every message on that page. A crash between the upserts and the checkpoint re-reads the same page. That replay is safe because the upsert is idempotent.

`conversations.history` returns newest first. `oldest` is the low watermark for the whole channel, not a value to advance after each page. Advance `oldest` to the greatest `ts` seen only when `response_metadata.next_cursor` is empty and that channel's thread pages are also complete.

## Phase 3. Channel backfill

Add `app/ingest/worker.py` with `backfill_public_channels(tenant_id, connection_id, workspace_id, client, graph)`.

For each public channel:

1. Read its `SyncState`. If `status` is `complete` and no incremental run was requested, skip it.
2. Page `history` with the stored `history_cursor` and `oldest`.
3. Normalize each message. `None` from a non-public type is a programming error here, because the list call already asked only for public channels. A `ValueError` from a malformed payload goes to the failed-record list and does not move the checkpoint.
4. Upsert the canonical message.
5. If `reply_count` is greater than zero, run Phase 4 for that parent before the channel checkpoint moves.
6. Store the next history cursor. When the cursor is empty, set `oldest` to the greatest message `ts` seen and mark the channel `complete`.

Also write one connection node the first time this workspace is synced:

- `(Tenant)-[:HAS_CONNECTION]->(Connection)`
- Connection id `{tenant}:slack:connection:{connection_id}`
- Properties: `tenant_id`, `source=slack`, `workspace_id`, `installed_at`
- Do not store the bot token on the node.

Tests, with a fake client and FalkorDB:

- Two history pages. After page one, `oldest` is unchanged and `history_cursor` is the first `next_cursor`.
- After the empty cursor, `oldest` equals the newest `ts` and a second backfill does not create more Message nodes.
- A thrown graph error on message two leaves the cursor where it was and leaves message one in the graph.
- A channel of type `private_channel` never reaches `upsert_message()`.

## Phase 4. Thread replies

`conversations.history` does not return every reply, and a later reply does not reappear as a new history row.

When a parent has `reply_count > 0`, page `conversations.replies` with that parent's sync node. The first row is the parent; upsert it through the same function, which fills a placeholder if the reply arrived earlier. Upsert each reply. Advance `replies_cursor` only after the page's writes succeed.

The channel is `complete` only after every such parent has an empty replies cursor.

Test: a parent with two reply pages, a crash after the first page, and a replay that results in one parent and both replies.

## Phase 5. Failed records

Add `:SyncFailure` nodes. Id: `{tenant}:slack:failure:{channel}:{ts}`.

Properties: `node_id`, `version`, `error_class`, `attempts`, `updated_at`. No excerpt, no body, no token.

`retry_failures(graph, client)` loads those nodes, fetches that single message again, and upserts it. Success deletes the failure node. The fifth failure leaves it in place with `attempts` set.

Test: one failed message retries without calling `conversations.history`.

## Phase 6. Edits and deletes

Add a FastAPI app at `app/main.py` and `POST /slack/events`.

- Read the raw body and verify `X-Slack-Signature` with `SLACK_SIGNING_SECRET` before parsing the payload as a command.
- Handle `url_verification` by returning the challenge.
- `message_changed` and `message_deleted` go through `normalize_slack_message()` and `upsert_message()`.
- Ignore events whose channel type is not `public_channel`. If the event has no channel type, resolve it from a public-channel allowlist built during backfill. An unknown channel is skipped and logged by id.
- Store the Slack `event_id` on a `:SlackEvent` node so a redelivery does not need a second write. The upsert is already safe if that check is missed.

Tests:

- A bad signature returns 401 and does not query the graph.
- A delete event blanks the excerpt and keeps `CONTAINS`.
- An older edit after a newer one does not change the excerpt.

Document the request URL and the bot scopes in `.env.example`: `channels:history`, `channels:read`, `channels:join`. Do not add private-channel scopes.

## Phase 7. Logs

One structured log line per page and per failed record: `tenant_id`, `workspace_id`, `channel_id`, `records`, `failed`, `duration_ms`, `error_class`.

The formatter test feeds a message body and a fake token and asserts neither string appears in the log line.

## Phase 8. One cited answer

Add `app/answer/channel_question.py`.

Input: `tenant_id`, `channel_id`, and a phrase. Match public Message nodes for that tenant whose excerpt contains the phrase, excluding `deleted = true` and `placeholder = true`. Return the excerpt, `occurred_at`, and `source_url` for each hit, newest first, limited to 20.

The answer text quotes only those excerpts and lists the URLs. If there are no hits, the answer says the graph has no matching public message. It does not call a model and does not invent a message.

This is the closing proof for Slack. A replaceable model adapter can sit in front of these hits later, after a provider is chosen.

Test: two messages, one deleted. The deleted excerpt is absent. The remaining hit includes its archive URL.

## Environment

Add these names to `.env.example` when the code that reads them lands. Values stay empty in git.

- `SLACK_BOT_TOKEN`
- `SLACK_SIGNING_SECRET`
- `FALKORDB_HOST`
- `FALKORDB_PORT`
- `FALKORDB_GRAPH`

## Order

Implement the phases in order. Phase 4 is part of the same worker as Phase 3, but its tests land before the worker is called complete. Phase 8 starts only after Phase 6 writes a tombstone the answer query can exclude.

After Phase 8, update `BUILD.md` and stop. The next connector is a separate plan.
