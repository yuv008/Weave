# Deploy

The AWS host is [docs/deployment.md](../docs/deployment.md): one edge task and one worker task in `ap-southeast-2`. The public origin is `https://we-9a1fc0e727ab4797bb20ad28efef5a43.ecs.ap-southeast-2.on.aws`. This folder's Compose file is the local stack.

`.env` is gitignored. `api` and `worker` load it with Compose `env_file` and `required: false`, so Compose 2.24 and newer still configure when the file is absent. Older Compose fails `docker compose config` until `.env` exists. Copy `.env.example` if you need a file, and do not commit real values.

When the Compose FalkorDB and Postgres services are the stores, set `FALKORDB_HOST=falkordb` and point `DATABASE_URL` at host `postgres` (`postgresql://weave:weave@postgres:5432/weave`). A hosted `FALKORDB_URL` or `DATABASE_URL` already in `.env` is used as written. The Google Drive service-account file is not copied into the image. `worker` mounts `./secrets` read-only, which is the default path `secrets/gdrive-service-account.json`.

Slack and GitHub webhooks are not a Compose service. They stay on `uvicorn app.main:app --host 127.0.0.1 --port 8000`. See `prompts/slack-events-url.md`.

Published ports: api `8765`, web `8080`. FalkorDB keeps `3000` and `6379`.

## Tunnels

One hostname for the web UI, one for the agent server. `web` and `api` resolve on the Compose network. Run each command after `cloudflared tunnel login`. On the host, use `http://127.0.0.1:8080` and `http://127.0.0.1:8765` instead.

```bash
cloudflared tunnel --name weave-web --hostname <web-host> --url http://web:8080
cloudflared tunnel --name weave-mcp --hostname <mcp-host> --url http://api:8765
```

## Cursor

The Windows installer writes this shape into `%USERPROFILE%\.cursor\mcp.json` and leaves other servers in that file. The hosted URL is the public origin plus `/mcp`. The sample below is the local Compose tunnel. `<token>` is a placeholder. Do not put a real token in this file.

```json
{
  "mcpServers": {
    "weave": {
      "url": "https://<mcp-host>/mcp",
      "headers": {
        "Authorization": "Bearer <token>"
      }
    }
  }
}
```
