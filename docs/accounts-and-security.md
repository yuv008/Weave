# Accounts and security

This page covers how companies, people, tokens, and limits work on the hosted pages, and what keeps one company's data away from another's. Deployment state is in [deployment.md](deployment.md). Parts marked *(account changes)* went live on 29 October 2026.

## Companies and people

- **Companies.** Signup creates a company. Its tenant id is a slug of the company name. `acme` and `real` are reserved. The address in `WEAVE_VIEWER_EMAIL` claims the home tenant `real` instead.
- **One owner.** Each company has exactly one owner. A unique index (`tenant_membership_one_owner`, migration 011) settles two signups racing for the same name, and the loser gets the normal "taken" error.
- **Members** *(account changes)* join through an owner's invite. A person can belong to several companies. The setup page switches between them, and every session is bound to one company at a time.
- **People in the graph.** The owner and each member are written as a `Person` at level 0 in the company graph. Levels, departments, and reporting lines cannot be edited on the setup page yet.

### Who can do what

| Action | Owner | Member |
| --- | --- | --- |
| Use Weave: Open Weave, pet download, own tokens | yes | yes |
| Connect or reconnect tools, pick the Drive folder or GitHub repository | yes | no |
| Company name, model key | yes | no |
| Invite, list, remove members; cancel invites | yes | no |

A member who opens a Connect link is sent back to `/setup?error=owner`. Owner-only API routes return 403.

## Sign-in

- **Passwords** are hashed with Argon2 and must be 10 to 200 characters.
- **Lockout.** After 8 failed logins for one address within 15 minutes, logins for that address are refused until the window passes.
- **Sessions** last 14 days. Each is a random token; only its sha256 is stored. The cookie `weave_session` is `HttpOnly`, `SameSite=Lax`, and `Secure` on https.
- **Sign out** *(account changes)*: `POST /logout` deletes the session row and clears the cookie.

### Email verification *(account changes)*

- Signup mails a link that works once, for 24 hours. `POST /verify/resend` mails a new one.
- Until the address is verified, a person can sign in and see the setup page, but cannot:
  - connect a tool,
  - mint a token or download the pet,
  - save a model key,
  - invite people.

  Those routes return 403 with `needs: verify`.
- Accounts made before 29 October 2026 count as verified (migration 014).
- Joining through an invite, or resetting a password, also proves the address.

### Password reset *(account changes)*

- `POST /forgot` gives the same answer for every address, so it does not show which addresses have accounts.
- The link works once, for 30 minutes.
- Setting a new password ends every session for that account and clears the lockout.

### Invites *(account changes)*

- An owner invites by email. The link works once, for 7 days.
- **Accepting:**
  - Someone signed in with the invited address joins and switches to that company.
  - Someone without an account picks a name and password, and the email is fixed from the invite.
  - Someone signed in with a different address is refused.
- **Removing a member** deletes their membership, ends their sessions for that company, and revokes their tokens there.

### Email

Mail goes through Gmail SMTP with an app password (`SMTP_USER`, `SMTP_PASSWORD`; optional `SMTP_HOST`, `SMTP_PORT`, `MAIL_FROM`):
- It is plain text and sent after the page has answered.
- A failed send is logged by error type only; the link is never logged.
- Gmail allows about 500 messages a day.
- Moving to SES or another sender only means replacing `app/connect/mailer.py`.

## Viewer tokens

The MCP server, `/answer`, the pet, and the Gmail extension use a bearer token (`wv_…`). Only its sha256 is stored, in `api_token`.

- **Minting.** "Open Weave" mints a token labelled `mcp`. The pet download mints one labelled `pet` and hands it over through a one-time setup code that lasts 15 minutes and travels in the installer's file name.
- **Expiry.** Tokens expire 30 days after they are made (migration 012). The database function `weave_token_owner` and the claims routes both refuse expired and revoked tokens. Tokens made before 28 October 2026 got 30 days from their creation date, so they expire between 14 and 22 November 2026.
- **Revoking.** `/setup` lists a person's own tokens by a short id (a hash prefix, never the hash) and can revoke one or all of them.
- **Caching.** The agent caches a token's caller for 60 seconds on `/answer` and `/wisp`, so a revoke or expiry takes effect within a minute.

## Model key

- **Own key** *(account changes)*. A company can save its own Groq key on `/setup`.
  - It is checked against Groq before it is stored. A rejected key is not saved.
  - From then on, `/answer`, `/wisp/mail`, and the watcher's drafts for that company use that key, with its own rate bucket.
  - The agent caches each company's key for 5 minutes, so a new or removed key takes effect within 5 minutes.
  - If Groq later rejects the saved key, the grant is marked `failed`, the request fails, and the setup page says so. Weave **never** falls back to the shared key, so a company's usage never moves onto the server bill.
- **Shared key.** Companies without their own key use the server's `GROQ_API_KEY`.
  - Each company gets `answer.llm.shared_daily_cap` requests a day (default 200) across `/answer` and `/wisp/mail`, counted in `model_usage` (migration 015).
  - Over the cap, the route returns 429 and points at `/setup`.
  - The home tenant is not counted.
  - If the count cannot be written, the request is allowed, and the per-token limits below still apply.

## Rate limits

nginx limits each client address. The address is the last `X-Forwarded-For` entry the load balancer added, so a sent header cannot change it.

| Paths | Limit per address |
| --- | --- |
| `POST` to `/login`, `/signup`, `/forgot`, `/reset`, `/verify/resend`, `/invite/accept`, `/invite/join` | 10 a minute, burst 5. Pages loaded with `GET` are not limited |
| `/mcp`, `/answer`, `/actions` | 120 a minute, burst 40 |
| `/wisp` | 300 a minute, burst 60 |

The agent also limits each token: `/answer` and `/wisp/mail` at 20 a minute, and `/wisp/cue` at 120 a minute. Over a limit, it returns 429 with `Retry-After`. These counters live in the edge process, which is fine while the edge runs as one task.

## Keeping companies apart

- **Tokens carry the company.** MCP, `/answer`, `/wisp`, the claims routes, and `/slack/decisions` all act in the company that owns the token, never the environment's tenant.
- **Slack events** go to the company whose saved grant owns the workspace, or to the home tenant only for its own workspace (`WEAVE_WORKSPACE_ID`). Other workspaces' events are dropped with a 200. If the lookup fails, the edge returns 503 so Slack retries, rather than guessing a company.
- **GitHub installs.** A GitHub connect is accepted only after:
  - the signed-in GitHub user's OAuth code is exchanged;
  - the install appears in that user's `GET /user/installations`;
  - no other company already holds that install.

  The browser picks `installation_id`, so it is never trusted on its own.
- **Connected sources.** Sources listed in `config/weave.yaml` are allowed for every company. A source a company connects adds a `connected_source` row that allows it for **that company only**. A lookup that misses reads Postgres again, so a source the worker added is seen by the edge.
- **Owner-only tables.** These tables have no row-level policy, and `weave_app` holds no grant on them:
  - `connection_secret`, `app_user`, `tenant_membership`, `app_session`, `login_attempt`,
  - `connected_source`, `model_usage`, `email_token`, `invite`.

  `tests/test_rls.py` checks this. Tenant tables use the row-level policies in migration 007.

### Private GitHub repositories

- **Who sees them.** A private repository is readable by the company **owners** only. A public one is readable by the whole company at level 0. A repository whose privacy cannot be read counts as private.
- **When it is checked.** The worker checks privacy on every poll. For a private repository it narrows the `connected_source` row, gives each owner a `group_membership` for the repository's ACL group, and bumps the company's `acl_epoch` so cached access refreshes. A row is narrowed automatically but never widened.
- **Items saved earlier.** Items stored before a policy changed keep their old tags until the source sends a new version. `python -m scripts.apply_source_acl [--dry-run]` brings items, source nodes, and chunks in line with every source's policy, and gives owners their memberships for owner-only sources.

## Known gaps

- **Private repositories** go to owners only. Access by GitHub collaborator is not built.
- **Levels, departments, and reporting lines** cannot be edited on the setup page.
- **Google Drive.** The Google OAuth app is in testing, so every Google account must be added as a test user by hand.
- **Edge state in memory.** Setup codes, OAuth state, and the per-token counters live in the edge process. A redeploy clears them, and the edge cannot run as more than one task until they move to a shared store.
- **The watcher and the GitHub webhook** cover the home tenant only. Other companies are polled.
