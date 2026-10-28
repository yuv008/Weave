# Deployment

**As of 28 October 2026.** Weave runs in the AWS project in the selected Region `ap-southeast-2` (Sydney). Neon stays in Ohio. FalkorDB stays in Mumbai. Nothing in this layout moves either database.

The home tenant is `real`. Its graph is `co:real`. `config/weave.yaml` still says `tenant: acme`, so the task environment sets `WEAVE_TENANT=real` and `WEAVE_TENANT_ID=real`. Other companies sign up on the hosted pages and get their own tenant and graph; see [accounts-and-security.md](accounts-and-security.md).

The public origin is `https://we-9a1fc0e727ab4797bb20ad28efef5a43.ecs.ap-southeast-2.on.aws`.

## Status

Both services are running. Running both costs about **$0.14 per hour** (about $3.40 a day) against the free plan credits in AWS Settings, Billing. `aws freetier get-account-plan-state` shows the credits left.

| What | State |
| --- | --- |
| `weave-edge` | Running image `weave:api-security2` (`sha256:d5aa1a80…`) |
| `weave-worker` | Running task definition `weave-worker:6`, image `weave:worker-security2` (`sha256:d5364908…`) |
| Migrations | 001–013 applied on Neon |
| Committed, not deployed | Per-company Groq key, sign out, password reset, email verification, team invites. They need migrations 014 and 015, the `SMTP_*` parameters, and a new edge image. See [Shipping the account changes](#shipping-the-account-changes). |

## What is running

ECS Express Mode creates one internet-facing load balancer that sends HTTPS to a single edge task. A second task polls the connectors and runs the watcher. It has a public address so it can reach Neon and FalkorDB, and its security group has no inbound rule.

| Service | Task | Command | Who can call it |
| --- | --- | --- | --- |
| `weave-edge` | 0.5 vCPU, 1 GB, one task | `edge` | The load balancer, on port 8080 |
| `weave-worker` | 1 vCPU, 2 GB, one task | `python scripts/run_worker.py` | Nobody. No load balancer |

Both tasks are Linux x86 on the `default` cluster. Each service stays at one task. Logs go to CloudWatch for 7 days, in `/weave/edge` and `/weave/worker`. Log lines do not include tokens.

`app.poll` is not the worker. It reads only environment variables and would ignore a token saved by a Connect button. `scripts/run_worker.py` reads `connection_secret`, polls every company that connected a tool, and starts the watcher. Do not run a second watcher beside it.

## Edge process

The edge image listens on port 8080. `deploy/edge.sh` starts three processes:

- the webhook, `uvicorn app.main:app` on `127.0.0.1:8000`, with proxy headers limited to `127.0.0.1`
- the agent, `python -m agents.mcp` on `127.0.0.1:8765`
- nginx, from `deploy/edge.nginx.conf`

`/mcp`, `/answer`, `/actions`, `/wisp`, and `/ops` go to the agent. Everything else goes to the webhook. That includes `GET /health`, `POST /slack/events`, `POST /github/webhook`, `/connect/...`, the sign-in pages, and the setup page. nginx forwards `X-Forwarded-Proto`, so OAuth callbacks stay `https://`.

nginx takes the client address from the last `X-Forwarded-For` entry the load balancer added, and rate-limits per address. Over a limit, it returns 429. The limits are in [accounts-and-security.md](accounts-and-security.md#rate-limits).

`POST /claims/commitment`, `POST /claims/figure`, `POST /claims/supersede`, and `POST /slack/decisions` require a viewer bearer token, and they act in the token's own company. A request with no token returns 401.

## Worker process

The worker image is the same Dockerfile with `INSTALL_EMBED=1`. That installs a CPU torch and the excerpt model. The first start downloads the model onto the task disk. The included 20 GB is enough. The task has no GPU.

Slack, Notion, Drive, and GitHub each poll for every company with a saved grant, and for the home tenant from the environment. A company's GitHub install is polled for every repository it can see. A private repository is kept to the company owners; see [accounts-and-security.md](accounts-and-security.md#private-github-repositories). The watcher runs for the home tenant only. Its tick is a dry run unless `watcher.send` is `slack`.

## Images and secrets

Images are private in ECR, repository `weave`. Each deploy pushes a new tag and the service is pinned to the image **digest**. The image does not contain `.env`.

Secret values live in Systems Manager Parameter Store as `SecureString` parameters under `/weave/real/`. The execution role `weaveTaskExecution` may read and decrypt anything under that path, so a new parameter needs no IAM change. The task role has no AWS permissions.

| Parameters | Used by |
| --- | --- |
| `DATABASE_URL`, `DATABASE_URL_UNPOOLED`, `FALKORDB_URL`, `WEAVE_SECRET_KEY` | both |
| `WEAVE_VIEWER_EMAIL`, `WEAVE_VIEWER_LEVEL`, `WEAVE_VIEWER_DEPT` | both |
| Slack, Notion, Drive and GitHub values for the home tenant | both |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | both. The entrypoint writes it to `/tmp/gdrive-service-account.json` |
| `SLACK_CLIENT_*`, `NOTION_CLIENT_*`, `GOOGLE_CLIENT_*` | edge (Connect buttons). The worker also has `GOOGLE_CLIENT_*` so Drive tokens refresh |
| `GITHUB_APP_ID`, `GITHUB_APP_PRIVATE_KEY` | both. They mint installation tokens |
| `GITHUB_APP_CLIENT_ID`, `GITHUB_APP_CLIENT_SECRET` | edge. They check that the signed-in GitHub user can reach an install |
| `GROQ_API_KEY` | both. It is the shared model key |
| `SMTP_USER`, `SMTP_PASSWORD` | edge, once the account changes ship. Gmail sends verify, reset, and invite mail |

`WEAVE_PUBLIC_URL` and `WEAVE_AGENT_URL` are both the public origin, with no path. Account links in email use `WEAVE_PUBLIC_URL`.

## Deploying

Build from this repository on a machine with Docker. Building from a branch that lacks live code would remove those routes from the site, so build from the branch that holds everything deployed.

1. **Apply migrations first.** Apply the files in `migrations/` in filename order on `DATABASE_URL_UNPOOLED`. Each is safe to run again. New code that reads a new column fails until its migration is in.
2. **Build:** `docker build --platform linux/amd64 -t <registry>/weave:<tag> .` builds the edge. Add `--build-arg INSTALL_EMBED=1` for the worker. Check that `deploy/edge.sh` has no CR characters inside the image. `.gitattributes` keeps `*.sh` on LF, but an older checkout on Windows can still carry CRLF, and the task would not start.
3. **Push:** on Windows, `docker login` to ECR can fail with "The stub received bad data" because the token is too long for the credential store. Push with a temporary `DOCKER_CONFIG` whose `config.json` holds an inline `auths` entry for the registry, then delete it.
4. **Edge:** `aws ecs update-express-gateway-service --cli-input-json file://spec.json`. The spec holds `serviceArn` and the full `primaryContainer` (image by digest, `environment`, `secrets`, port 8080, the `edge` command). Start from `describe-express-gateway-service` so nothing is dropped.
5. **Worker:** register a new revision of the `weave-worker` task definition with the new image digest, then run `aws ecs update-service --cluster default --service weave-worker --task-definition weave-worker:<n>`.
6. **Check:** wait for the deployment's `rolloutState` to be `COMPLETED`, then run the checks below.

## Shipping the account changes

The per-company model key, sign out, reset, verification, and invites are committed but not deployed. In order:

1. Apply `migrations/014_accounts_email.sql` and `migrations/015_model_usage.sql`. 014 marks accounts made before 29 October 2026 as verified.
2. Store `SMTP_USER` (the Gmail address) and `SMTP_PASSWORD` (a Google app password) under `/weave/real/`. Add both to the edge container's `secrets`.
3. Build, push, and roll out a new edge image. The worker needs no new image.
4. Check:
   - a new signup gets a verification email;
   - `/forgot` mails a reset link;
   - an owner can invite a second address, and the invitee can join;
   - `/setup/status` shows `model: own` after a Groq key is saved.

## Checks

`python -m scripts.check_stores` runs as a one-off edge task so it uses the same network path as the service. A pass prints `postgres_pooled=ok`, `postgres_direct=ok`, and `falkordb=ok`.

From another machine:

- `GET /health` returns `{"ok":true}`
- `GET /`, `/guide`, and `/downloads/WeavePetSetup.exe` return 200
- `POST /claims/commitment` with no token, or an unknown one, returns 401
- `GET /setup/tokens` without a session returns 401
- a burst of wrong-password `POST /login` requests turns into 429; `GET /login` does not
- a worker log line shows a finished Slack, Notion, Drive, or GitHub page with `failed=0`
- the watcher tick stays a dry run

## Provider consoles

These settings live in the provider consoles, not in this repository. Each one points at the public origin above:

- Slack event URL: `<origin>/slack/events`. The edge task has to be awake when Slack sends its URL check.
- GitHub webhook: `<origin>/github/webhook`. It covers the home repository only. Other companies' repositories are polled.
- OAuth callbacks: `<origin>/connect/slack/callback`, `/connect/notion/callback`, `/connect/google/callback`, `/connect/github/callback`.
- GitHub App: **Request user authorization (OAuth) during installation** must be on, with the callback above. Without it, GitHub sends no `code` and every GitHub connect is refused.
- Google OAuth app: still in testing. Each Google account must be added as a test user before it can connect Drive.

A Connect button stays disabled until that provider's client id and secret are in Parameter Store and the edge is redeployed. HubSpot stays unavailable.

## Building on this laptop

- C: runs close to full. When it fills, Docker Desktop stops mid-build and Windows may clear `%TEMP%`. `docker builder prune -f` usually frees about 20 GB. Keep build files outside `%TEMP%`.
- If Docker Desktop reports "Copying the main distribution failed … being used by another process", the WSL service still holds Docker's disk. Restart Windows, or run `wsl --shutdown` and `Restart-Service WslService -Force` in an administrator PowerShell, then open Docker Desktop.

## Stopping it

Delete the Express Mode service `weave-edge` to remove the load balancer. Delete the `weave-worker` service, or set it to zero tasks, to stop the poll. Leaving either one up continues to spend the hourly cost above. The images, the `/weave/real/` parameters, the execution role, and the task definitions stay after a stop.
