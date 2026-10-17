# Deployment

**As of 18 October 2026.** Weave runs in the AWS project in the selected Region `ap-southeast-2` (Sydney). Neon stays in Ohio. FalkorDB stays in Mumbai. Nothing in this layout moves either database.

The live tenant is `real`. The company graph is `co:real`. `config/weave.yaml` still says `tenant: acme`, so the task environment sets `WEAVE_TENANT=real` and `WEAVE_TENANT_ID=real`.

As of 18 October 2026 the tasks are stopped. `weave-worker` is kept at zero tasks. The Express Mode service `weave-edge` is deleted, and with it the load balancer and the public hostname. The images in ECR, the `/weave/real/` parameters, the execution role, and the worker task definition are still there. The next deploy creates a new hostname.

Running both services costs about **$0.14 per hour**. With the tasks and the load balancer gone, what remains is image storage, about a cent a day. The free plan credits are in AWS Settings, Billing. Stopping the tasks stops the Fargate and address charges. The load balancer keeps costing about $0.028 per hour until the Express Mode service is deleted.

## What is running

One internet-facing load balancer, created by ECS Express Mode, sends HTTPS to a single edge task. A second task polls the connectors and runs the watcher. It has a public address so it can reach Neon and FalkorDB, and its security group has no inbound rule.

| Service | Task | Command | Who can call it |
| --- | --- | --- | --- |
| `weave-edge` | 0.5 vCPU, 1 GB, one task | `edge` | The load balancer, on port 8080 |
| `weave-worker` | 1 vCPU, 2 GB, one task | `python scripts/run_worker.py` | Nobody. No load balancer |

Both tasks are Linux x86 on the `default` cluster. Each service stays at one task. Logs go to CloudWatch for 7 days, in `/weave/edge` and `/weave/worker`. Log lines do not include tokens.

`app.poll` is not the worker. It reads only environment variables and would ignore a token saved by a Connect button. `scripts/run_worker.py` reads `connection_secret` and starts the watcher. Do not run a second watcher beside it.

## Edge process

The edge image listens on port 8080. `deploy/edge.sh` starts three processes:

- the webhook, `uvicorn app.main:app` on `127.0.0.1:8000`, with proxy headers limited to `127.0.0.1`
- the agent, `python -m agents.mcp` on `127.0.0.1:8765`
- nginx, from `deploy/edge.nginx.conf`

`/mcp`, `/answer`, `/actions`, `/wisp`, and `/ops` go to the agent. Everything else, including `GET /health`, `POST /slack/events`, `POST /github/webhook`, `/connect/...`, and the setup page, goes to the webhook. nginx forwards `X-Forwarded-Proto`, so OAuth callbacks stay `https://`.

`GET /` is the setup page. Connect stays disabled until the OAuth client id and secret for that provider are in Parameter Store and the edge task is redeployed. HubSpot stays unavailable.

`POST /claims/commitment`, `POST /claims/figure`, `POST /claims/supersede`, and `POST /slack/decisions` require the viewer bearer token. A request with no token returns 401.

## Worker process

The worker image is the same Dockerfile with `INSTALL_EMBED=1`. That installs a CPU torch and the excerpt model. The first start downloads the model onto the task disk. The included 20 GB is enough. The task has no GPU.

The command is `python scripts/run_worker.py`. Slack, Notion, Drive, and GitHub each poll when their settings are present. The watcher tick is a dry run unless `watcher.send` is `slack` on a real tenant.

## Images and secrets

Images are private in ECR, repository `weave`, tags `api` and `worker`. The image does not contain `.env`.

Secret values live in Systems Manager Parameter Store as `SecureString` parameters under `/weave/real/`. The execution role `weaveTaskExecution` may read and decrypt them. The task role has no AWS permissions. Parameters are only the names that already have a value: the database URLs, `FALKORDB_URL`, `WEAVE_SECRET_KEY`, the viewer fields, and the Slack, Notion, Drive, and GitHub values the worker already uses. Empty OAuth client ids are omitted. The Drive service-account JSON is stored as `GOOGLE_SERVICE_ACCOUNT_JSON`. The entrypoint writes it to `/tmp/gdrive-service-account.json` and points `GOOGLE_SERVICE_ACCOUNT_FILE` at that path.

`WEAVE_PUBLIC_URL` and `WEAVE_AGENT_URL` are both the public origin, with no path.

## Checks

From the laptop, the files in `migrations/` are applied in filename order on `DATABASE_URL_UNPOOLED`. They are safe to run again. Do not run `scripts.seed_fixture` or `scripts.issue_demo_tokens`.

`python -m scripts.check_stores` runs as a one-off edge task so it uses the same network path as the service. A pass prints `postgres_pooled=ok`, `postgres_direct=ok`, and `falkordb=ok`.

From another machine:

- `GET /health` returns 200
- `POST /claims/commitment` with no bearer token returns 401
- `GET /` loads the setup page, with the connectors unavailable until the OAuth app values exist
- a worker log line shows a finished Slack, Notion, Drive, or GitHub page, and no pool timeout
- the watcher tick stays a dry run

After those pass, the local webhook and the local `scripts/run_worker.py` are stopped so tenant `real` is not synced twice.

## Provider consoles

These edits are in the provider consoles, not in this repository:

- Slack event URL: `https://we-ac39f9f25fad4e1799447ff520a571f3.ecs.ap-southeast-2.on.aws/slack/events`
- GitHub webhook: `https://we-ac39f9f25fad4e1799447ff520a571f3.ecs.ap-southeast-2.on.aws/github/webhook`

The edge task has to be awake when Slack sends its URL check.

When the OAuth apps are created, register the same origin for:

- `/connect/slack/callback`
- `/connect/notion/callback`
- `/connect/google/callback`
- `/connect/github/callback`

Put the client id and secret into Parameter Store under `/weave/real/` and redeploy the edge task. Until then the Connect buttons stay disabled.

## Stopping it

Delete the Express Mode service `weave-edge` to remove the load balancer. Delete the `weave-worker` service to stop the poll. Leaving either one up continues to spend the hourly cost above.
