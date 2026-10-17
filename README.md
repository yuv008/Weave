# Weave

Weave is a company knowledge graph of people, decisions, commitments, and the sources behind them. A gated answer agent replies only with evidence the caller is allowed to see. People ask from Slack, or from Cursor through the MCP server.

## Setup

Use Python 3.12 (`requires-python` is `>=3.12`). From this directory, install with `pip install -e ".[dev]"`.

Copy `.env.example` to `.env`. Put keys only in `.env`. That file is gitignored. Fill `GROQ_API_KEY`, `DATABASE_URL`, and either `FALKORDB_URL` or `FALKORDB_HOST` and `FALKORDB_PORT`. Fill the `SLACK_*` names from the example: `SLACK_BOT_TOKEN`, `SLACK_SIGNING_SECRET`, `SLACK_WORKSPACE_DOMAIN`, and `SLACK_USER_MAP`. Add `OPS_TOKEN` in `.env` when you want the ops page. Do not commit any of these values.

On Neon, `DATABASE_URL` is the pooled endpoint. `DATABASE_URL_UNPOOLED` is the direct endpoint. Each Python process also keeps a small client pool in `shared/pg.py` (`psycopg-pool`) so repeated queries reuse an open connection to PgBouncer. Migrations and the watcher outbox still use the direct URL; normal agent, ingest, and Wisp cue traffic use the pooled URL. After upgrading dependencies, restart `python -m agents.mcp`, the webhook app, and `scripts/run_worker.py` if they are already running.

## Connect

The webhook app serves the setup page at `GET /`. A person saves a company name, connects Slack, Notion, Google Drive, or GitHub, and then chooses Open Weave. That mints a viewer token and a `pet.config.json` the desktop pet reads for its cue URL and token. HubSpot stays on the page and cannot be connected yet. Apply `migrations/008_connection_secret.sql` on the direct database, and set `WEAVE_SECRET_KEY` plus the provider client ids in `.env` (the names are in `.env.example`) before the buttons will leave this machine. Claim routes `POST /claims/*` and `POST /slack/decisions` require the viewer bearer token.

## AWS

The Sydney host is [docs/deployment.md](docs/deployment.md). One edge task serves the setup page, the webhooks, and the agent. One worker task runs `scripts/run_worker.py`. On 18 October 2026 the tasks and the load balancer were stopped. The images and the saved parameters remain. Starting again creates a new public hostname.

## Docker Compose

`docker compose up` starts five services from `docker-compose.yml`: `api` on port 8765 (`python -m agents.mcp`), `worker` (`scripts/run_worker.py`), `web` on port 8080, `falkordb`, and `postgres`.

`api` and `worker` load `.env` with Compose `env_file`. The image build does not copy `.env`. When the Compose databases are the stores, set `FALKORDB_HOST=falkordb` and set the host in `DATABASE_URL` to `postgres`. A hosted `FALKORDB_URL` or `DATABASE_URL` already in `.env` is used as written.

## Public URLs

`deploy/README.md` documents the two cloudflared hostnames: one for the web UI and one for MCP. Cursor uses `https://<mcp-host>/mcp` with `Authorization: Bearer <token>`. The token comes from `python -m scripts.issue_demo_tokens`. Keep it out of git.

Slack and GitHub webhooks stay on `uvicorn app.main:app` at port 8000. They are separate from the five Compose services. See `prompts/slack-events-url.md`.

## Tests

Install optional extras with `pip install -e ".[dev,embed]"` (`embed` is only needed for poll-time chunk embeddings).

Offline checks for Postgres pools, gate, and Wisp cue (including multiple chunk matches):

`python -m pytest tests/test_pg_pool.py tests/test_rls.py tests/test_wisp_cue.py tests/test_gate.py -q`

Full offline suite: `python -m pytest tests/ -q` (loads `.env` via `tests/conftest.py` when present).

## Migrations

Apply the SQL files in `migrations/`, including `migrations/007_rls.sql`, on the direct unpooled connection before you rely on row-level security. App queries that know a tenant use the `weave_app` role. The owner connection still bypasses the policies, so migrations and seeds on that connection still see every row.

## Groq

`config/weave.yaml` sets `answer.llm.provider` to `groq` (model `openai/gpt-oss-120b`). `answer.llm.rpm` defaults to 30 calls per minute in that process. The key is `GROQ_API_KEY`.

`provider: none` is the rule-based path. Tests use it. The load script below needs the API process on that same path.

## Ops

`GET /ops` requires `Authorization: Bearer` set to `OPS_TOKEN`. When `OPS_TOKEN` is unset the route is hidden and returns 404. The page shows permission-sync age, ingestion lag, queue depth, and p95 latency for `WEAVE_TENANT` only. It does not show message text.

## Load test

```bash
python -m scripts.load_answer --users 50 --url http://127.0.0.1:8765 --as sam
```

The API process for that run must use the rule-based agent (`answer.llm.provider: none`) so the run does not spend the Groq key. The script only sends `POST /answer`. It does not write to the database, and it does not print the bearer token.

## Demo beats

These five beats are already in the build. The fake company is tenant `acme` on graph `co:acme`.

1. **Cursor / MCP, flow 1.** Ask "I'm starting the SSO endpoint. Which SSO provider should I use?" The agent calls `get_decision` and recommends Provider B, citing Dev's `#eng-sso` message (quote, date, and Slack link) and noting that it replaced Provider A. Priya's token gets the same decision with the roadmap citation only.
2. **Priya status email, flow 2.** Priya asks "Draft a status update for Acme about SSO". The draft is one sentence per fact she can see, with numbered footnotes, stored as a proposal. Accepting it keeps the email as a draft.
3. **Impact watcher, flow 3.** A change event becomes a gated notice. Priya sees that her commitment "SSO will be live for Acme by Nov 15." is at risk, and that "Use Provider B for SSO." is now in effect via roadmap #42. Provider A is not named in her notice. Everyone else gets nothing. On `acme` the send stays a dry run.
4. **Slack mention.** An `app_mention` in a channel is answered as the channel principal (lowest member level, channel group plus org-wide). If the answer has no claim, or some evidence was filtered, the reply adds one line telling the asker to DM the bot. A channel that includes a level-0 or unmapped member cannot cite `#eng-sso`.
5. **Slack DM.** A 1:1 DM is answered as the mapped person (`Person.slack_id`, then `SLACK_USER_MAP`). A DM mapped to Sam can cite `#eng-sso`. One reply is sent per event.

Mention replies and the watcher's Slack send path are tested with a stubbed HTTP client. A live post to a real workspace has not been confirmed.

## Demo video

Record this by hand. There is no video file in the repo.

1. Connect Cursor to `https://<mcp-host>/mcp` with a bearer token.
2. Ask as Sam (flow 1, the SSO provider question, with the `#eng-sso` citation).
3. Ask as Priya (flow 2, the Acme status email).
4. Show a watcher notice for Priya (the at-risk Acme commitment and Provider B).
5. Open `GET /ops` with the ops bearer token and show sync age, ingestion lag, queue depth, and p95.

## Isolation

`migrations/007_rls.sql` adds the `weave_app` role. A query that knows its tenant sets that role for one transaction, and a row is visible only when its `tenant_id` matches. FalkorDB stays split by graph name, `co:{tenant}`. A second seeded tenant is not in the repo.
