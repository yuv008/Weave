# Weave build log

Living record of what is actually in the repository. `AGENTS.md` is the product constitution. Connector plans: [docs/slack-plan.md](docs/slack-plan.md), [docs/notion-plan.md](docs/notion-plan.md), [docs/drive-plan.md](docs/drive-plan.md). This file is the status.

Update this file when a slice lands or a decision changes. The Grok development prompt in `prompts/grok-session-summary.md` reads this file and refreshes the summary every 30 minutes.

## Latest summary

- **As of:** 14 October 2026
- **Built:** Public Slack, Notion, and Google Drive are connected for this demo. Slack includes a live delete tombstone. Drive has one Google Doc in FalkorDB. Notion has one shared page: it has a source URL, a `HAS_SOURCE` edge, and a title. The page body hash is empty, so the excerpt is empty.
- **Not built:** OAuth, a web UI, and a model-backed answer.
- **Next step:** Add a paragraph to the shared Notion page and poll again if a cited answer should quote that page. Full message and document bodies are still not stored.
- **Open:** Where full message bodies live. Sync state is a temporary FalkorDB `SyncState` node.

## Decisions

- FalkorDB and FastAPI are the working choices. FastAPI serves `POST /slack/events`.
- Nango is not chosen. Slack is called directly. The bot token comes from the environment. There is no OAuth install flow yet.
- Groq, named in `AGENTS.md`, is not chosen. Grok is a development partner, not the product runtime.
- The first connector is Slack, and only public channels. Private channels, group DMs, and DMs are skipped.
- This is a single-tenant demo. It is not permission-safe.
- Full message text is not stored. The graph keeps a SHA-256, a 280-character prefix, and a source URL.
- Person ids are `{tenant}:slack:person:{team}:{user}`. Conversation ids are `{tenant}:slack:conversation:{team}:{channel}`. Message ids are `{tenant}:slack:message:{channel}:{ts}`.
- Message versions are zero-padded Slack timestamps so string order matches time order.
- An equal version keeps the stored excerpt and re-merges edges. An older version does not write.
- A reply may arrive before its parent. The parent is a placeholder until the real message fills it.
- Deletes blank the excerpt and leave the edges. Earlier versions are not kept.
- The Slack code path has run against live workspace `T0C8H9PPXU7`. Human `message.channels` delivery and a later delete are both confirmed on the same `#social` node.
- Notion and Drive call their APIs directly. Notion users and Drive owners are not merged into Slack `Person` nodes. `NOTION_VERSION` is `2026-03-11`.

## What is built

| Piece | Location | Behavior |
| --- | --- | --- |
| Canonical public Slack message | `app/models/canonical.py` | Source-explicit fields only. `visibility` is fixed at `demo_single_tenant`. |
| Normalizer | `app/slack/normalize.py` | Accepts `public_channel` only. Pads timestamps, hashes the full text, stores a 280-character prefix, unwraps `message_changed`, and tombstones `message_deleted` without keeping the previous body. |
| Graph upsert | `app/graph/upsert.py` | Merges Tenant, Conversation, Person, and Message. Edges: `HAS_CONVERSATION`, `CONTAINS`, `AUTHORED`, `REPLIES_TO`. Excerpt travels only as a Cypher parameter. |
| Indexes | `ensure_indexes()` | Id indexes for Tenant, Conversation, Person, and Message, plus `Message.tenant_id`. Safe to call twice. |
| Client helper | `app/graph/client.py` | Reads `FALKORDB_HOST`, `FALKORDB_PORT`, and `FALKORDB_GRAPH`. |
| Slack Web API client | `app/slack/client.py` | Public-channel list, history, and replies. Token stays in the Authorization header. `429` and `5xx` retry. Other `4xx` responses do not. |
| Checkpoints and backfill | `app/ingest/state.py`, `app/ingest/worker.py` | `SyncState` nodes. `oldest` advances only after the history cursor and thread cursors are exhausted. `newest_ts` remembers the high watermark across crashes. |
| Connection | `write_connection()` | `(Tenant)-[:HAS_CONNECTION]->(Connection)`. The bot token is not stored on the node. |
| Failed messages | `:SyncFailure` | Retried through `conversations.replies`, not `conversations.history`. The fifth failure stays on the list. |
| Page logs | `app/ingest/log.py` | Tenant, workspace, channel, counts, duration, and error class. Message text and tokens are dropped. |
| Events | `app/main.py`, `app/slack/events.py` | `POST /slack/events` checks the Slack signature before reading the command. Public channel type `channel` is accepted. Deletes blank the excerpt and keep edges. |
| Cited answer | `app/answer/channel_question.py` | Excerpt matches for one public channel, newest first, limit 20. Deleted and placeholder messages are excluded. No model call. |
| Canonical document | `app/models/canonical_document.py`, `app/graph/upsert_document.py` | Shared by Notion and Drive. Hash, 280-character prefix, and URL. Equal version keeps the excerpt. Older versions do not write. |
| Notion connector | `app/notion/` | REST client, block text, poll of shared pages, tombstones for `in_trash`. |
| Drive connector | `app/gdrive/`, `scripts/check_drive_access.py` | Read-only service account. Google Docs in one folder. Other MIME types are skipped. |
| Document answers | `app/answer/notion_question.py`, `app/answer/drive_question.py` | Excerpt matches with source URLs. Deleted documents are excluded. No model call. |
| Tests | `tests/test_normalize.py`, `tests/test_upsert.py`, `tests/test_slack_client.py`, `tests/test_worker.py`, `tests/test_events.py`, `tests/test_answer.py`, `tests/test_log.py`, `tests/test_notion.py`, `tests/test_drive.py` | Checkpoint order, thread resume, stale versions, rejected signatures, tombstones, and cited answers for Slack, Notion, and Drive. |

Local check on 14 October 2026: `53 passed`. FalkorDB ran in Docker container `weave-falkordb`, host port `6380`. `.env.example` defaults the port to `6379`. Use `FALKORDB_PORT=6380` for that container.

## What is not built

- Slack OAuth install flow
- Person names, decisions, `MENTIONS`, files, reactions, and attachments
- A model-backed answer
- A web UI and per-user ACLs

## Connected sources

Tokens and keys stay in `.env` or `secrets/` and are never copied into this file. The three demo connectors below have each completed one live write into FalkorDB.

### Notion

- An internal Notion integration is configured. `/v1/users/me` succeeded: the integration is a bot in workspace **Yuvraj Sanghai's Space**.
- `.env` has `NOTION_WORKSPACE_ID=de87560e-92b0-81e5-b99a-000341cccd77` and `NOTION_CONNECTION_ID=notion-main`. The integration token is in `.env` as `NOTION_TOKEN` (not recorded here).
- `NOTION_VERSION` is pinned to `2026-03-11`.
- A live poll stored **1** document. It has a source URL and a `HAS_SOURCE` edge. The excerpt is empty because the shared page has a title and no supported block text.
- Plan: [docs/notion-plan.md](docs/notion-plan.md). Direct REST API only; no Nango.

### Google Drive

- The Drive API is enabled in Google Cloud Console.
- Planned auth: read-only service account **weave-drive-reader**, Viewer on one shared folder, no project roles.
- Connector code is in `app/gdrive/`. The access check passed for the shared folder, and one Google Doc is in FalkorDB.
- `.env` has `GOOGLE_DRIVE_CONNECTION_ID=gdrive-main` plus the service-account path and folder id. The key file stays in `secrets/` and is gitignored.
- Plan: [docs/drive-plan.md](docs/drive-plan.md). No Nango.

### Repository setup (not connector code)

- `secrets/`, `tools/`, and `logs/` are in `.gitignore`. `.env` was already ignored.
- `tools/cloudflared.exe` was added for the Slack events tunnel. `uvicorn` was installed into `.venv` for `POST /slack/events`.
