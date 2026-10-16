# Deployment plan

**As of 16 October 2026.** This is the plan for running Weave without a laptop in the loop. It is not a record of a deploy that has already happened.

The live tenant is `real`. The company graph is FalkorDB `co:real`. Postgres is Neon. Both stay where they are. `docker-compose.yml` only starts local copies of those databases and is not the deploy target. The portable app stack is `Dockerfile` and `compose.deploy.yaml`.

`config/weave.yaml` keeps `tenant: acme`, so anything that falls back to the file stays on the fixture. The host environment sets `WEAVE_TENANT=real` and `WEAVE_TENANT_ID=real`.

## What "deployed" means

A public Slack message or GitHub event lands in `co:real` while no local process is running, and Cursor can call the agent over HTTPS with the existing viewer token.

The static page in `frontend/index.html` is not part of this deploy. It still shows the hardcoded Acme story and does not call `POST /answer`.

## Will Lightning AI credits work?

Yes, for a demo, on a **CPU Studio**. Credits pay for the machine while it is switched on. Weave does not need a GPU. `BAAI/bge-small-en-v1.5` runs on CPU inside the worker. Spending the credits on a GPU machine would burn them without making the demo faster in a way we need.

Lightning can host this shape:

- One Studio runs several servers at once. That matches the three processes below.
- A FastAPI app on a port can be exposed with **Deploy on public ports**. The URL looks like `https://<studio>.lightning.ai:<port>` and is HTTPS, which Slack and GitHub require.
- Outbound access from the Studio reaches FalkorDB Cloud, Neon, Slack, Notion, Drive, and GitHub.

Three Lightning behaviors decide whether the demo holds:

1. **Serverless sleep.** A Studio can sleep when the port is idle and wake on the next request. Slack must get a signed response within about 3 seconds, including the URL challenge. A cold start misses that window, Slack retries, and the event subscription shows a failure. For the demo, leave the Studio awake and turn serverless auto-sleep off on the webhook port.
2. **Restarts.** On the free Studio allowance, an active Studio restarts on a short cycle (about 4 hours). A restart drops the webhook process until something starts it again. A plan whose Studio does not restart is the one to use if the URL has to stay up for an afternoon. After any restart, confirm both health URLs before posting another test message.
3. **Interruptible machines.** Spot machines can be taken back. Use an on-demand CPU Studio for the webhook, not an interruptible one.

Leave Lightning's own token or basic auth **off** on both public ports. Slack and GitHub authenticate with their signatures, not with a Lightning token. Cursor sends `Authorization: Bearer <viewer token>`. An extra proxy login would reject those callers. Our agent process already checks the bearer token. The claim routes must learn that check before they are reachable (see the gap below).

Credits run out while the Studio is on. Sleep or stop the Studio when the demo is over, and turn it on again before the next one. The first wake downloads the embedding model unless the Studio disk still has it.

A Lightning Studio is the right host for this demo. A small VM is the same plan if the credits run out or the Studio restarts become a problem. The processes, secrets, and checks below do not change between those two hosts.

## Processes

| Process | Command | Who can call it |
| --- | --- | --- |
| Webhooks | `uvicorn app.main:app --host 0.0.0.0 --port 8000` | Slack `POST /slack/events`, GitHub `POST /github/webhook` |
| Agent | `python -m agents.mcp --host 0.0.0.0 --port 8765` | Cursor at `/mcp`, and `POST /answer`, with the viewer bearer token |
| Poll | `python -m app.poll --every 300` | Nothing public. Slack runs incrementally. A connector with missing settings is skipped. |
| Watcher | `python -m agents.watcher --every 60` | Nothing public. It reads `WEAVE_TENANT` and stays a dry run. |

On Lightning, bind both web apps to `0.0.0.0` and expose ports 8000 and 8765. On a VM, bind them to `127.0.0.1` and let Caddy own 443.

The poll repeats every 5 minutes, which is inside `acl_stale_minutes` (15). A longer gap makes the gate hide the tenant even though the data is still in the graph. `python -m app.poll` loads the environment, refuses `WEAVE_TENANT=acme`, and polls Slack, Notion, Drive, and `GITHUB_OWNER`/`GITHUB_REPO`. Leave `GITHUB_REPO` on `portfolio` until the token can see the private Weave repository. A successful page refreshes `source_sync` and embeds excerpts, so the poll image installs the `embed` extra.

The watcher stays a dry run. `config/weave.yaml` has `watcher.send: dry_run`. It claims `change_event` rows, and that table is empty on tenant `real`, so each tick is idle until a supersede exists.

## Files that run on any host

| File | Role |
| --- | --- |
| `Dockerfile` | One image definition. `INSTALL_EMBED=0` is the web apps. `INSTALL_EMBED=1` is the poll and watcher. |
| `compose.deploy.yaml` | Webhooks, agent, poll, and watcher. Databases stay outside. |
| `deploy/Caddyfile` | Optional HTTPS proxy in front of the two web apps. |
| `.dockerignore` | Keeps `.env`, secrets, and git out of the image. |

From the repo root, with `.env` present and the Drive JSON at the path in `GOOGLE_SERVICE_ACCOUNT_FILE`:

```bash
docker compose -f compose.deploy.yaml up --build
```

Webhook health is `http://<host>:8000/health`. Agent health is `http://<host>:8765/health`. Neither requires a token.

On Render, use the same Dockerfile. The webhook and agent are web services with the commands in the table above, and Render's `$PORT` in place of 8000 and 8765. The poll and watcher are background workers. Give the poll worker at least 2 GB. Set `INSTALL_EMBED=1` for that build.

On a VM, the same compose file is the deploy. Add the proxy only when the names are real:

```bash
WEBHOOK_HOST=hooks.example.com AGENT_HOST=brain.example.com docker compose -f compose.deploy.yaml --profile proxy up --build
```

Caddy then answers on 80 and 443 and fetches certificates for those names.

## Close these before the URL is public

1. Require the viewer bearer token on `POST /claims/commitment`, `POST /claims/figure`, `POST /claims/supersede`, and `POST /slack/decisions`. Those four routes currently accept any caller. Slack and GitHub stay signature-checked and do not use that token. Do this before the webhook port is reachable from the internet.

`answer.llm.provider` stays `none`. Turning on a model host is a separate change.

## Secrets

Put these in the Studio's environment settings, or in the VM secret file. Do not bake them into an image and do not commit them.

- `FALKORDB_URL`, `DATABASE_URL`, `DATABASE_URL_UNPOOLED`
- `WEAVE_TENANT=real`, `WEAVE_TENANT_ID=real`, `WEAVE_CONFIG=config/weave.yaml`
- `WEAVE_VIEWER_EMAIL`, `WEAVE_VIEWER_LEVEL=1`, `WEAVE_VIEWER_DEPT=engineering`
- Every Slack, Notion, Drive, and GitHub variable named in `.env.example`

Upload the Drive service-account JSON into the Studio (or mount it on the VM) and set `GOOGLE_SERVICE_ACCOUNT_FILE` to that path.

Do not run these on the host:

- `python -m scripts.issue_demo_tokens` mints tokens for Acme people.
- `python -m scripts.seed_fixture` writes the fixture graph `co:acme`.
- `python -m scripts.touch_sync` refuses every tenant except `acme`. Freshness for `real` comes from the poll.

The viewer token already stored in `api_token` for tenant `real` is the one Cursor uses. Copy it into the Studio as a secret named for your own notes. Do not print it into the Studio logs.

## Build on the Studio

Use Python 3.12.

```bash
pip install -e .
pip install -e ".[embed]"
```

The second install is only needed in the worker environment. On one Studio it is simplest to install both into the same environment. The first poll downloads `BAAI/bge-small-en-v1.5`. Later polls reuse it if the Studio disk survives.

If you later split images, `weave-api` is `pip install .` and `weave-worker` is `pip install .[embed]`.

## Database

Neon already has the tables from the 16 October work. From the Studio, once, apply any missing file against `DATABASE_URL_UNPOOLED`, in this order:

1. `migrations/001_contract.sql`
2. `migrations/002_ingest_record.sql`
3. `migrations/002_agent_auth.sql`
4. `migrations/003_chunk_fts.sql`
5. `migrations/004_audit_log.sql`
6. `migrations/005_agent_memory.sql`

The statements are `CREATE TABLE IF NOT EXISTS` or `ADD COLUMN IF NOT EXISTS`, so running them again is safe. Then:

```bash
python -m scripts.check_stores
```

Expect the stores check to pass. Expect `python -m scripts.validate_graph --tenant real` to still report the known gap: the non-viewer Slack person has no email, level, or department, and the older `CONTAINS` and `AUTHORED` edges are not contract edges. A deploy should not add a new class of error.

## Start order

1. Apply migrations and run `python -m scripts.check_stores`.
2. Start the webhook process on port 8000. Open `GET /health` from outside the Studio.
3. Start the agent process on port 8765. Open its health URL, then one authenticated `POST /answer` from outside.
4. Expose both ports in the Studio. Leave Lightning's extra login off.
5. Run `python -m app.poll` once by hand. Confirm it prints counts and exits 0.
6. Start `python -m agents.watcher --tenant real --every 60` in the background. Confirm a tick prints counts and does not send a Slack DM.
7. Point Slack and GitHub at the webhook URL only after steps 2 and 5 have succeeded.

## Point the providers at the Studio

Slack's event request URL becomes:

```text
https://<studio>.lightning.ai:8000/slack/events
```

Slack sends a signed `url_verification` challenge. `apply_slack_event` returns the challenge. The Studio has to be awake. The bot stays installed with `channels:history`, `channels:read`, `channels:join`, `users:read`, and `users:read.email`.

GitHub, on `yuv008/portfolio` only:

```text
https://<studio>.lightning.ai:8000/github/webhook
```

Content type JSON. Secret equal to `GITHUB_WEBHOOK_SECRET`. Subscribe to Issues and Pull requests, not Pushes.

Cursor's MCP URL:

```text
https://<studio>.lightning.ai:8765/mcp
```

Header: `Authorization: Bearer <viewer token>`.

After both providers show a successful delivery, stop the local Cloudflare tunnel.

## Prove the deploy

From a machine other than the Studio:

1. Post a short message in `#social`. After the event, or after one poll, the message is in `co:real` and Postgres has a chunk for it.
2. Open an issue or pull request on `yuv008/portfolio`. The webhook returns 200, and the record is stored.
3. Ask, through Cursor or `python -m scripts.ask --url https://<studio>.lightning.ai:8765`, what the Drive test document says. The card cites that document and the Slack events message.
4. `POST /claims/commitment` with no token is refused.
5. The watcher log shows a dry-run tick and no Slack message.

## After the demo

Stop the Studio, or let it sleep, so credits stop. Starting it again means: start the three processes, confirm both health URLs, then post one Slack message to prove the event URL still answers.

Serve `frontend/index.html` only after it calls `POST /answer` with the viewer token.

Set `watcher.send` to `slack` only when a real `change_event` exists and that DM should actually send. An LLM provider in `answer.llm` is a separate change.

## If the credits run out

Run the same three processes on a 4 GB CPU VM. Caddy terminates HTTPS and routes `/slack/events` and `/github/webhook` to port 8000, and `/mcp`, `/answer`, and `/actions/` to port 8765. Bind the apps to `127.0.0.1`. The secrets, the poll, and the proof steps stay the same. Update the Slack and GitHub URLs to the VM host.

## Rollback

Clear the Slack event URL and the GitHub webhook, or point them back at the tunnel. Stop the Studio. Leave `co:real` and Neon in place. Writes use `MERGE` on stable ids, so a bad process does not require restoring the databases.
