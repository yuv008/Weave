# Grok prompt: set the Slack events URL

**Status:** live `message.channels` delivery and a later delete were confirmed while a Cloudflare tunnel was running. That hostname is temporary. If the tunnel process stops, Slack needs the new request URL. Do not print the signing secret.

Paste this into the Grok bot. Slack MCP can see the workspace. It cannot open the Slack app's Event Subscriptions page unless the bot also has a browser.

```text
Set the Slack Event Subscriptions request URL for the Weave app so public-channel edits and deletes reach Weave.

Read `.env` and `BUILD.md` in the Weave project. Do not print `SLACK_BOT_TOKEN` or `SLACK_SIGNING_SECRET`. Do not commit `.env`. Do not add private-channel, group DM, or DM event subscriptions. Do not use Nango.

The events route already exists: `POST /slack/events` in `app/main.py`. It checks the Slack signature, answers the `url_verification` challenge, and applies `message`, `message_changed`, and `message_deleted` for public channels. The app process does not load `.env` by itself. Load those variables into the environment before starting it.

Do this in order:

1. From the Weave folder, load `.env` into the process environment. Install `uvicorn` into `.venv` if it is missing. Start:

   .\.venv\Scripts\python -m uvicorn app.main:app --host 127.0.0.1 --port 8000

   Leave that process running. FalkorDB must stay available at the host and port in `.env`.

2. Open a public HTTPS tunnel to `http://127.0.0.1:8000`. The request URL is the tunnel origin plus `/slack/events`, for example `https://<tunnel-host>/slack/events`. It must be HTTPS. Slack rejects plain HTTP.

3. Confirm the tunnel reaches the local app. A POST without a valid Slack signature must return HTTP 401. Do not send the signing secret or the bot token in that check.

4. Save that request URL on the Slack app:
   - Open https://api.slack.com/apps and select the app installed in the connected demo workspace. The workspace id is in `.env` as `WEAVE_WORKSPACE_ID`. Do not print it.
   - Event Subscriptions → Enable Events.
   - Request URL: the `https://<tunnel-host>/slack/events` URL from step 2.
   - Wait until Slack shows the URL as verified. Verification sends a signed `url_verification` challenge. If it fails, the usual cause is that uvicorn was started without `SLACK_SIGNING_SECRET` in its environment, or the tunnel is not forwarding to port 8000.
   - Under Subscribe to bot events, add only `message.channels`.
   - Do not add `message.groups`, `message.im`, `message.mpim`, or `message.mpim`.
   - Save changes. Reinstall the app to the workspace if Slack asks.

5. Prove one live delivery. In a public channel the bot has joined, edit or delete a short test message. Then check FalkorDB for that message id. An edit replaces the excerpt. A delete sets `deleted=true`, blanks the excerpt, and keeps the `CONTAINS` edge. Report the channel id, message ts, and those graph fields. Do not paste the message text or any secret.

If you cannot open the Slack app settings, stop after step 3 and reply with only:
- the exact request URL
- the one bot event to add: `message.channels`
- the page where it is saved: Event Subscriptions for the app in the connected demo workspace

Do not invent a verified URL. If Slack has not shown the URL as verified, say it is not verified.
```
