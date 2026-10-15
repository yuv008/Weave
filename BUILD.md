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
- The Slack code path has run against the connected demo workspace. Human `message.channels` delivery and a later delete were both confirmed on the same public-channel message. The workspace id stays in `.env`.
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

## Shared contract and fake graph (15 October 2026)

Agents are built against a seeded fake company while ingestion moves onto the contract. Nothing in `app/` changed.

| Piece | Location | Behavior |
| --- | --- | --- |
| Schema constants | `shared/schema.py` | Labels, edges, properties and statuses from the architecture doc. Existing ids and property names are kept; contract labels (`Item`, `Source`) sit next to `Message`, `Document` and `Conversation`. |
| Ids, ACL, time | `shared/ids.py`, `shared/acl.py`, `shared/timefmt.py` | Id builders reproduce the formats above. ACL strings are `slack:C123`, `github:org/repo`, `notion:x`, `gdrive:x`, `org:all`. Timestamps are UTC at seconds precision. |
| Config | `config/weave.yaml`, `shared/config.py` | Per-source `min_level`, `cascade_policy`, `authority`, `org_wide`; `bge-small-en-v1.5` at 384 dimensions; `likely_threshold`; `hide_provisional`. Org-wide items carry their own group plus `org:all`. |
| Postgres | `migrations/001_contract.sql` | `group_membership`, `source_sync`, `tenant_acl`, `change_event` outbox, `chunk` with `vector(384)`. |
| Fixture | `fixtures/acme.py`, `python -m scripts.seed_fixture` | Five people; the five matrix sources from page 15 of the architecture doc (public eng channel, eng team repo, `#eng-leads`, `#leadership`, leadership budget sheet) plus four story sources (`#eng-sso`, `#deal-acme`, Sales deals, roadmap repo). Provider A superseded by B, Acme commitment, budget figure, projections, one ChangeEvent. Graph `co:acme`. The Postgres seed replaces the tenant's memberships, sync rows and chunks, and refuses any tenant but `acme`. |
| Validator | `python -m scripts.validate_graph` | Evidence, required properties, ACL strings, statuses, supersession, edge directions. `--fixture` runs offline. |
| First query | `python -m scripts.query_active_decision --as sam` | Active SSO decision with the evidence Sam may see. |

Open: whether `REPLIES_TO` stays in the contract.

## Permission gate (15 October 2026)

| Piece | Location | Behavior |
| --- | --- | --- |
| Read-only graph | `agents/graph.py` | Exposes only `ro_query`; FalkorDB rejects writes sent that way. Private graph opens only for its owner on a private surface. |
| Principal | `agents/gate/principal.py` | Tenant, kind (person or channel), surface, level, groups, freshly synced sources, `acl_epoch`, visibility hash. |
| Rules | `agents/gate/rules.py` | Own groups plus `org:all`; level 2+ inherits reports' groups; an item is hidden when any of its source groups has no sync within 15 minutes, including org-wide sources; channel principal takes the lowest member level. |
| Filter | `agents/gate/visible.py` | `item_visible`, `projection_visible` (approved only), and `keep_visible_claims`, a WITH step because FalkorDB rejects `EXISTS {}` and pattern comprehensions in WHERE. Parameters: `$gate_level`, `$gate_groups`, `$gate_fresh`. |
| Resolver | `agents/gate/resolve.py`, `agents/gate/stores.py` | Token and surface to principal. ACL snapshot from Postgres in one connection; levels and reporting lines from the graph. Errors say only "not authorized". |
| Tokens | `migrations/002_agent_auth.sql`, `python -m scripts.issue_demo_tokens` | SHA-256 hashes only; tokens printed once. |
| Fixture freshness | `python -m scripts.touch_sync` | Re-stamps `acme` sync times before a demo; refuses other tenants. |
| Tests | `tests/test_gate.py`, `tests/test_gate_live.py` | The 25 cells of the page-15 matrix, 20 story-source cells, channel audience, privacy flip (Maya by DM vs. in a channel with Sam), draft projection, revocation, stale and unsynced org-wide sources, tokens, read-only. Live tests run with `WEAVE_LIVE_TESTS=1`. |

## Agent tools (15 October 2026)

Read-only tools from the architecture doc's tool catalogue, in `agents/tools/`. Each takes a `ToolContext` (principal from the gate, `ReadOnlyGraph`, config, clock) and never a user id or surface. Each returns node ids, citations (quote, author, date, source, link; projections as summary with the source "restricted") and the edges walked.

| Tool | Behavior |
| --- | --- |
| `get_decision` | Active decision by topic, repo or id, or what was in force `as_of` a date. Conflicting active decisions side by side, "likely" under the threshold, the replaced decision only if visible, projection summary when no raw evidence is visible, otherwise "try search_context". |
| `trace_decision` | Decision to evidence items, authors, sources and threads. Hidden hops are counted for the audit only. |
| `find_owner` | `OWNS` owners plus top experts scored at query time over visible items, with recency decay. |
| `get_figures` | Latest active figures with as-of date, sources, supersession history and a stale badge after `figure_stale_days`. |
| `get_dependencies` | `NEEDS`, `DEPENDS_ON`, `BLOCKED_BY` up to three hops, one gated hop per query, labelled by owner department. |
| `impact_of` | Work resting on a decision or change id, with owners to notify. The changed decision shows as restricted when the caller cannot see it. `affected_owners` (not a tool) picks recipients for the impact watcher. |
| `dept_rollup` | Open cross-team requests, blocks and at-risk counts over visible edges only. |
| `search_context` | Postgres full-text over chunks filtered by the gate before ranking (`migrations/003_chunk_fts.sql`), re-checked in the graph, expanded one hop. Vector search joins by reciprocal rank fusion once an embedder is set. |

Contract additions: dependency edges carry `evidence_ids`; config gains `hidden_sources_notice` (off), `figure_stale_days`, `expertise_half_life_days`. Tests: `tests/test_tool_*.py` (scratch FalkorDB graph, live with `WEAVE_LIVE_TESTS=1`) and `tests/test_tools_read_only.py` (offline).

## MCP server (15 October 2026)

The tools above over the Python MCP SDK (Streamable HTTP, stateless, JSON responses), in `agents/mcp/`.

- **Auth:** every request carries `Authorization: Bearer <token>`. The gate resolves it on the MCP surface, which is private, so the caller is the token's owner. The tenant and graph come from the token. A missing, unknown or revoked token gets 401 "not authorized", with no other detail.
- **Tool list:** built from the registry. Descriptions read "Use when / Do not use when", every argument is documented, and every tool is marked `readOnlyHint`. Input schemas come from the signatures, so they never offer a user, tenant or surface.
- **Errors:** bad arguments return a tool error with a plain message. Anything else returns "internal error (request id)"; details go only to the server log.
- **Audit:** one `audit_log` row per call (`migrations/004_audit_log.sql`), with the caller, tool, arguments, node ids, hidden count, outcome and time.

Run it locally:

```
python -m scripts.issue_demo_tokens --rotate | Set-Content -Encoding ascii .env.tokens   # once; never print tokens
python -m scripts.touch_sync --every 300                                                 # second terminal, keeps acme fresh
python -m agents.mcp                                                                     # http://127.0.0.1:8765/mcp
python -m scripts.mcp_flow1                                                              # the day-3 check as Sam
```

Cursor (user-level `~/.cursor/mcp.json`, never committed):

```json
{ "mcpServers": { "weave-sam": {
    "url": "http://127.0.0.1:8765/mcp",
    "headers": { "Authorization": "Bearer <WEAVE_TOKEN_SAM from .env.tokens>" } } } }
```

Flow 1: ask Cursor "I'm starting the SSO endpoint. Which SSO provider should I use?". It calls `get_decision` and recommends Provider B, citing Dev's `#eng-sso` message (quote, date, Slack link) and noting that it replaced Provider A. Priya's token gets the same decision with the roadmap citation only.

Tests: `tests/test_mcp_server.py` (offline: auth, tool list, arguments, error masking, audit, one principal per request) and `tests/test_mcp_live.py` (real HTTP server, SDK client, scratch graph; `WEAVE_LIVE_TESTS=1`).

## What is not built

- Slack OAuth install flow
- Person names, decisions, `MENTIONS`, files, reactions, and attachments
- A model-backed answer
- A web UI and per-user ACLs

## Connected sources

Tokens and keys stay in `.env` or `secrets/` and are never copied into this file. The three demo connectors below have each completed one live write into FalkorDB.

### Notion

- An internal Notion integration is configured. `/v1/users/me` succeeded: the integration is a bot in its Notion workspace. The workspace name stays out of this file.
- The Notion workspace id, connection id, and token stay in `.env`. None of those values are recorded here.
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
