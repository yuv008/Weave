# Grok prompt: fill the Weave Slack environment

**Status:** this setup already ran for workspace `T0C8H9PPXU7`. Keep the prompt for a new workspace. Do not print tokens.

Paste this into the Grok bot that has the Slack MCP connected. The bot cannot see this Cursor session. It can see the Slack workspace through MCP.

```text
You are setting up the local Weave environment for one public Slack workspace. Weave is the project in this folder. Read `.env.example` and `BUILD.md` before you write anything.

Use the Slack MCP tools you already have. Do not invent tool results. Do not add a second connector. Do not call Nango. Do not request private channels, group DMs, or DMs.

Goal: write a local `.env` file next to `.env.example` with these keys filled:

FALKORDB_HOST=localhost
FALKORDB_PORT=6380
FALKORDB_GRAPH=weave
SLACK_BOT_TOKEN=
SLACK_SIGNING_SECRET=
SLACK_WORKSPACE_DOMAIN=
WEAVE_TENANT_ID=demo
WEAVE_CONNECTION_ID=slack-main
WEAVE_WORKSPACE_ID=

Where each value comes from:

1. WEAVE_TENANT_ID and WEAVE_CONNECTION_ID are already chosen above. Keep them unless I gave different names. They must not contain ":" or "/".
2. From Slack MCP, find the workspace team id and the workspace URL.
   - team id looks like T0123ABCD. That is WEAVE_WORKSPACE_ID.
   - The URL looks like https://acme.slack.com. SLACK_WORKSPACE_DOMAIN is the subdomain, acme.
   - If no tool returns the team directly, call auth.test if a raw Slack Web API tool exists. Otherwise read team_id from a public channel or user object the MCP returns.
3. List public channels only, to prove the connection can see them. Do not read message bodies for this task. Do not join channels.
4. SLACK_BOT_TOKEN and SLACK_SIGNING_SECRET are Slack app credentials. MCP will not reveal them.
   - If a tool result already contains a bot token starting with xoxb-, use that token and do not print it back to me.
   - Otherwise stop and ask me to paste the Bot User OAuth Token and the Signing Secret from https://api.slack.com/apps, in one reply. The app needs bot scopes channels:history, channels:read, and channels:join. After I paste them, continue. Do not ask me for anything Slack MCP already resolved.
5. Write `.env`. If `.env` already exists, update these keys and leave every other line unchanged. Never edit `.env.example` with real values. Never commit `.env`. Never put the token or signing secret into BUILD.md, a summary, or a log.

Then reply with a redacted check only:

- tenant id
- connection id
- workspace id
- workspace domain
- public channel count
- bot token: present or missing, and only whether it starts with xoxb-
- signing secret: present or missing

Do not show the token, the signing secret, or any message text. Stop after that check. Do not run a backfill unless I explicitly ask.
```
