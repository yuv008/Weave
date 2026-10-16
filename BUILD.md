# Weave build log

Living record of what is actually in the repository. `AGENTS.md` is the product constitution. Connector plans: [docs/slack-plan.md](docs/slack-plan.md), [docs/notion-plan.md](docs/notion-plan.md), [docs/drive-plan.md](docs/drive-plan.md), [docs/github-plan.md](docs/github-plan.md). This file is the status.

Update this file when a slice lands or a decision changes. The Grok development prompt in `prompts/grok-session-summary.md` reads this file and refreshes the summary every 30 minutes.

## Latest summary

- **As of:** 15 October 2026
- **Built:** Public Slack, Notion, and Google Drive are connected for this demo. New connector writes go to `co:{WEAVE_TENANT}` and only for sources listed in `config/weave.yaml`. Slack messages are `:Message:Item` with `IN_SOURCE` and `AUTHORED_BY`. GitHub issues use the shared issue id and the `Issue` label. `POST /slack/decisions` merges one `Decision` per matching message. `SyncState`, `SyncFailure`, and `SlackEvent` are Postgres `ingest_record` rows. Person nodes still lack `name`, `email`, `level`, and `dept`. `AUTHORED` and `CONTAINS` remain, so `validate_graph` does not report `errors=0`.
- **Not built:** OAuth, a web UI, and a model-backed answer. GitHub file contents, diffs, and review threads are out of scope. `Commitment`, `Figure`, supersede, and projections are not built. The live demo channel is not in `config/weave.yaml`, so it is not written.
- **Next step:** Both builders add the live Slack channel to `config/weave.yaml` if that workspace should be ingested. Full message and document bodies are still not stored.
- **Open:** Where full message bodies live. Person email, level, and dept are still missing, so the validator reports those people.

## Decisions

- FalkorDB and FastAPI are the working choices. FastAPI serves `POST /slack/events` and `POST /slack/decisions`. The decisions route merges a caller-supplied statement onto each matching message and does not supersede.
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
- GitHub calls the REST API directly at version `2026-03-10`. One repository. Issues and pull requests, plus issue comments, are `Document` nodes. File contents are not read. No live GitHub write has been confirmed. See `docs/github-plan.md`.

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
| Slack decisions | `app/answer/decision.py`, `POST /slack/decisions` | Caller supplies one statement and one quote. Up to 20 matches come back as JSON. The quote must sit in the excerpt. No `Decision` node is written. |
| Canonical document | `app/models/canonical_document.py`, `app/graph/upsert_document.py` | Shared by Notion, Drive, and GitHub. Hash, 280-character prefix, and URL. Equal version keeps the excerpt. Older versions do not write. A GitHub comment may create a placeholder issue. |
| Notion connector | `app/notion/` | REST client, block text, poll of shared pages, tombstones for `in_trash`. |
| Drive connector | `app/gdrive/`, `scripts/check_drive_access.py` | Read-only service account. Google Docs in one folder. Other MIME types are skipped. |
| Document answers | `app/answer/notion_question.py`, `app/answer/drive_question.py`, `app/answer/github_question.py` | Excerpt matches with source URLs. Deleted documents are excluded. No model call. |
| GitHub connector | `app/github/`, `scripts/check_github_access.py` | One repository. Issues, pull requests, and issue comments. Poll watermark, failure retry, and signed `POST /github/webhook`. No live write yet. |
| Tests | `tests/test_normalize.py`, `tests/test_upsert.py`, `tests/test_slack_client.py`, `tests/test_worker.py`, `tests/test_events.py`, `tests/test_answer.py`, `tests/test_log.py`, `tests/test_notion.py`, `tests/test_drive.py`, `tests/test_github.py` | Checkpoint order, thread resume, stale versions, rejected signatures, tombstones, and cited answers for Slack, Notion, Drive, and GitHub. |

Local check on 15 October 2026: `66 passed`. FalkorDB ran in Docker container `weave-falkordb`, host port `6380`. `.env.example` defaults the port to `6379`. Use `FALKORDB_PORT=6380` for that container.

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
- **Tool list:** built from the registry. Descriptions read "Use when / Do not use when", every argument is documented, and read tools are marked `readOnlyHint` (memory and propose tools from Part 5 are not). Input schemas come from the signatures, so they never offer a user, tenant or surface.
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

## Answer agent and memory (15 October 2026)

A question goes in and a cited answer card comes out (`agents/answer/`). No LLM provider is chosen yet: with `answer.llm.provider: none`, a rule-based router, planner and composer run every flow. The LLM versions sit behind one `LLM` protocol and are tested with a scripted fake.

- **Steps:**
  1. Re-gate the conversation so far.
  2. Route the question to an intent: lookup, provenance, ownership, impact or draft.
  3. Match its subjects (names and aliases, longest first).
  4. Recall memory.
  5. Run the tool loop.
  6. Compose claims.
  7. Validate them and build the card.
  8. Write one `audit_log` row (`kind='answer'`), the episode and the conversation turns.
- **Intents:** each intent allows only its own tools (`ALLOWED` in `intents.py`). For example, a lookup cannot draft an email.
- **Guards** (same for any planner):
  - at most `answer.max_tool_calls` (6) calls, recall included
  - the tool must be allowed for the intent
  - an id argument must already have appeared in this run (subject match, recall or an earlier result)
  - arguments are checked by `agents/tools/invoke.py`, which MCP uses too

  A refused step is recorded in the trace and never reaches a tool.
- **Playbooks:** named steps per role and intent, versioned in `playbooks`. Built-in defaults apply when none is stored. `python -m scripts.seed_playbooks` adds the API-question, provenance, sales-draft (flow 2) and impact playbooks.
- **Citation contract:** a claim stands only on evidence returned in this run. Evidence can be a citation item, an approved projection, or a graph edge (`path:src|rel|dst`, for structural facts such as ownership). `answer.citation_policy` is `drop` (default) or `flag`, which keeps the claim marked unverified.
- **Card:**
  - the answer and the claims, each with its evidence: quote, author, date, source and link, or the restricted summary
  - the path edges
  - freshness, e.g. "Active since Oct 12, replaces Provider A (Oct 3)", or a figure's as-of date and stale badge
  - confidence: the lowest among cited facts, with "likely" labels and conflicts side by side
  - `hidden_count` only with `hidden_sources_notice`
- **Drafting** (`agents/tools/drafts.py`, `mode="propose"`): both tools write a `tasks` row in state `proposed` and nothing else.
  - `draft_email` re-gates each fact and writes one sentence per fact with numbered footnotes.
  - `notify_owner` keeps only the evidence both people may see, using the recipient's own principal, and refuses if none is left.
  - `confirm_action` (`agents/answer/actions.py`) is for people only and is not a tool. It records accept, edit or reject on the proposal and as feedback on the episode. An accepted note becomes a ready `slack_dm` task for `slack_sender` (Part 6 sends it), with the evidence re-checked at confirm time. An accepted email stays a draft.
- **Memory** (`agents/memory/`, `migrations/005_agent_memory.sql`): conversations and turns, episodes, playbooks and tasks, namespaced by (tenant, user, agent).
  - `remember` stores node ids plus a note of at most 200 characters, never facts.
  - `recall_memory` searches the caller's own episodes with Postgres full text and re-gates every id with `visible_node_ids`. An entry with any id the caller can no longer see is dropped without a trace.
  - Earlier turns of a conversation are re-gated the same way; a turn that fails shows "earlier answer no longer available".
  - Nothing is recalled or kept for channels.
  - `claim_task` uses `FOR UPDATE SKIP LOCKED`.
- **Cache:** read-tool results are cached for `answer.cache_ttl_seconds` per visibility hash. The hash includes `acl_epoch`, so a revocation always misses.

Run it (the server from the MCP section also serves `POST /answer` and `POST /actions/{id}/confirm` on the web surface):

```
python -m scripts.seed_playbooks                                        # once
python -m scripts.ask --as sam "Which SSO provider should I use?"       # flow 1 card
python -m scripts.ask --as priya "Draft a status update for Acme about SSO"
python -m scripts.confirm_action <proposal id> accept --as priya        # or edit --text "...", or reject
```

Adding an LLM provider: write one class with `complete(messages, tools, json_schema) -> LLMReply` in `agents/answer/llm.py`, construct it in `build_components`, and set `answer.llm` in `config/weave.yaml`.

Tests:
- `tests/test_answer_offline.py`: the 15-question set in `tests/data/questions.yaml` (intent and first tool), the guards, the validator, the card, the LLM path and the HTTP routes.
- `tests/test_answer_live.py`: flows 1 and 2, provenance, impact, notify, confirm and audit on the scratch graph.
- `tests/test_memory.py`: offline, plus live Postgres under a throwaway tenant.

## Impact watcher (flow 3) (15 October 2026)

A worker in `agents/watcher/` turns `change_event` rows into gated notices, customer-update drafts and Slack DMs. It reads the graph and writes only tasks and audit rows. Nothing in `change_event` or `migrations/` changed.

- **Claiming events** (`outbox.py`): one transaction per event, `FOR UPDATE SKIP LOCKED` on the direct Neon endpoint. A normal exit marks the event processed. An error rolls back, so the event stays pending and is retried on the next tick. Workers never wait on each other.
- **Per recipient** (`watcher.py`):
  - `affected_owners` (ungated) only picks whom to ask. Each person then runs `impact_of` with their own principal on the Slack DM surface.
  - Only live work in their own result that lists them as owner counts. Someone with nothing of their own gets nothing; the audit records a count, never ids.
  - The notice is built from that result only. A root they cannot see reads "A decision your work depends on was replaced"; its statement, evidence and links never reach them.
  - Two tasks are written with deterministic ids, so a replayed event writes nothing new:
    - `notice:{event}:{person}`: a ready `slack_dm` for `slack_sender`.
    - `handoff:{event}:{person}`: a ready `draft_customer_update` for `answer`, ids only.
  - One `audit_log` row per recipient (`kind='watcher'`, outcome `notified`, `filtered` or `error`).
- **Handoff** (`handoff.py`): the recipient is resolved again and every id re-gated. The answer agent then drafts as them (`intent=draft`, seeded subjects and the at-risk commitment). The result is a `proposal_email` created by the recipient in their own episode, confirmed with `scripts.confirm_action`. The handoff ends `done`, `expired` or `failed`.
- **Sender** (`sender.py`): claims ready `slack_dm` tasks, from the watcher and from Part 5's `confirm_action`. It re-gates the evidence for the recipient at send time and expires the DM if none is visible. Once the linked handoff is done it appends "Draft customer update ready to review: {proposal id}".
- **Order:** each tick runs events, then handoffs, then sends (`worker.py`), so the first DM already points at the draft.

Dry run vs Slack (`watcher:` in `config/weave.yaml`):
- `send: dry_run` (the default) stores `{transport, to, text, at}` as `delivery` on the task and marks it `sent`.
- `send: slack` posts with `conversations.open` and `chat.postMessage` using `SLACK_BOT_TOKEN`. It is used only for a non-fixture tenant and a person with a real Slack user id. The token stays in the Authorization header.
- `acme` is always a dry run; its Slack ids are fake.

What each recipient sees on the fixture:

| Recipient | Notice |
| --- | --- |
| Priya | Her commitment "SSO will be live for Acme by Nov 15." (due 2026-11-15) is at risk, and "Use Provider B for SSO." is now in effect (via roadmap #42). Provider A is never named: it is restricted for her. No `#eng-sso` id, quote or link. The draft cites `#deal-acme`, roadmap #42 and the approved projection. |
| Everyone else | Nothing. Only Priya owns work resting on Provider A. |

Run it:

```
python -m agents.watcher --tenant acme --once                 # one tick; --every [SECONDS] keeps going
python -m agents.watcher --steps events                       # only claim events; also handoffs, sends
python -m scripts.flow3 --tenant acme --reset                 # demo: replay the fixture event, print Priya's view
```

`--reset` refuses any tenant but `acme`. It marks the fixture's change event pending again. It deletes that event's `notice:` and `handoff:` tasks, the draft each handoff produced, and that draft's episode and conversation. The run on 15 October 2026 printed the notice above, `Handoff [done]` and a draft that states both facts.

Tests:
- `tests/test_watcher_offline.py`: wording per change kind; the notice only from the recipient's own result; nothing for a recipient who sees nothing; deterministic ids and replays; ids-only handoff payload; handoff expiry; sender re-gate; tick order and the draft pointer.
- `tests/test_watcher_live.py`: flow 3 on the scratch graph with Neon memory under a throwaway tenant. Only Priya is notified. There is no Provider A and no `#eng-sso` id, quote or link. Her evidence is visible to her. One handoff gives a `proposal_email` by Priya. The dry-run DM carries the pointer. A second run and a replay add nothing.
- `tests/test_watcher_outbox_live.py`: Neon `change_event` under a throwaway tenant. Two connections get different rows, a rollback leaves the row pending, a commit marks it processed, and three threads take each row once.

## What is not built

- Slack OAuth install flow
- Person names, decisions, `MENTIONS`, files, reactions, and attachments
- An LLM provider adapter (the answer agent runs on rule-based components)
- Semantic memory, nightly consolidation, vector recall, and LLM-written conversation summaries
- Live Slack sending has not run against a real tenant (the watcher's `send: slack` path is tested with a stubbed HTTP client only)
- A retry limit for an event that keeps failing (it is logged, audited as `error` and retried every tick)
- A web UI and per-user ACLs
- A live GitHub write. The connector code is in the repo. `scripts/check_github_access.py` has not been run against a token.

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
