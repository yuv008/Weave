# Plan: GitHub issues and pull requests

**Status:** implemented in code. Live access has not been run. Slack, Notion, and Drive remain the connectors with a confirmed FalkorDB write. This plan does not replace the Notion follow-up recorded in `BUILD.md`.

Ingest issues and pull requests from one repository into FalkorDB as `Document` nodes, including their issue comments, and answer one cited question from stored excerpts.

`BUILD.md` records what already exists.

## Done when

- A read of one configured repository upserts each issue and pull request, and each of its issue comments, into FalkorDB.
- Re-running the poll does not duplicate `Document` nodes.
- The sync watermark moves only after that page of issues and their comment pages have been written.
- A newer `updated_at` replaces the excerpt and hash. An older write does not.
- Closing or merging keeps the excerpt. `status` becomes `closed` or `merged`.
- A deleted issue or comment becomes a tombstone: `deleted=true`, excerpt blank, edges kept.
- The worker never calls another repository, and never calls the Contents or pulls-files APIs.
- One phrase over that repository returns matching excerpts and GitHub URLs. Deleted documents are excluded. Closed ones are not.
- Tests cover idempotency, the watermark, tombstones, a rejected webhook signature, and the cited answer. No live GitHub call in unit tests.

## Stay fixed

- Nango is not part of this plan. Call the GitHub REST API directly.
- Pin `X-GitHub-Api-Version` to `2026-03-10`. A missing version header defaults to `2022-11-28`. Do not use that default.
- One repository, named by `GITHUB_OWNER` and `GITHUB_REPO`. A token that can see other repositories is not a reason to list them.
- Auth is a fine-grained personal access token with read access to Metadata, Issues, and Pull requests. Do not request Contents. Do not build a GitHub App or an OAuth install flow.
- Read-only. No comments, labels, closes, or merges are posted back to GitHub.
- Issues and pull requests share GitHub's number space. One node per number. A pull request is detected only when `pull_request` is an object. A missing or null `pull_request` is an issue.
- In scope for text: the issue or pull request title and body, and issue comments from `GET /repos/{owner}/{repo}/issues/{number}/comments`.
- Out of scope: file contents, diffs, commits, review comments, review submissions, Discussions, Projects, Actions, wikis, releases, and code scanning. Those need different APIs and a different product decision.
- Do not store the full body. Keep the hash, the 280-character prefix, and `html_url`.
- Do not create `Person` nodes and do not merge GitHub logins into Slack people. Store the login on the document as `author_external_id`.
- Do not describe the result as permission-safe. The scope remains `demo_single_tenant`. A private repository's excerpts are visible to anyone who can query this graph. Pick a repository that is acceptable for that demo.
- The model provider stays unchosen. The cited answer is assembled from retrieved excerpts only.
- The token and webhook secret stay in `.env`. Never log them, commit them, or copy them into `BUILD.md`.

## Graph model

Reuse `Document` and `upsert_document()`. Extend `CanonicalDocument` so `source` may be `github`. Notion and Drive behavior stays as it is.

Node id for an issue or pull request: `{tenant}:github:document:{repo_id}:issue:{number}`.

Node id for an issue comment: `{tenant}:github:document:{repo_id}:comment:{comment_id}`.

`repo_id` is the numeric id from `GET /repos/{owner}/{repo}`, not `owner/name`. Renames must not create a second node.

Connection id: `{tenant}:github:connection:{connection_id}`.

Relationships:

- `(Tenant)-[:HAS_CONNECTION]->(Connection)`
- `(Connection)-[:HAS_SOURCE]->(Document)` for both the issue and its comments
- `(issue Document)-[:CONTAINS]->(comment Document)`

`CONTAINS` already means a conversation contains a message. Here it means the issue contains the comment. Do not point `CONTAINS` from a comment back to the issue.

Required properties on `Document`: the fields already written for Notion and Drive, plus:

- `source=github`
- `workspace_id` = numeric `repo_id`
- `external_id` = `issue:{number}` or `comment:{comment_id}`
- `kind` = `issue`, `pull_request`, or `comment`
- `status` = `open`, `closed`, or `merged` on the issue or pull request. Empty on a comment.
- `author_external_id` = `user.login` when GitHub sends a user. Empty when the user is missing.
- `parent_external_id` set only on comments, to `issue:{number}`
- `version` = sortable UTC form of `updated_at`
- `visibility=demo_single_tenant`

Add these fields as optional on `CanonicalDocument` with empty defaults. Notion and Drive leave them empty, and their upserts do not gain a `CONTAINS` edge.

`closed` and `merged` are not tombstones. `deleted=true` is only for a GitHub delete or a confirmed `404` on a record this connector has already stored.

## Phase 0. Access test (no graph write)

Add `scripts/check_github_access.py`.

Read `GITHUB_TOKEN`, `GITHUB_OWNER`, and `GITHUB_REPO` at the process edge. `GET /repos/{owner}/{repo}` and `GET /repos/{owner}/{repo}/issues?state=open&per_page=1`. Print the numeric repository id, whether it is private, and the open-issue count from the response. Do not print titles, bodies, or the token.

Exit non-zero on `401`, `403`, `404`, or a missing owner or repo.

Run manually after the token is created and the repository is chosen.

## Phase 1. GitHub REST client

Add `app/github/client.py`. The token is accepted only through the constructor. The worker reads `GITHUB_TOKEN` from the environment and never logs it.

Send these headers on every request:

- `Authorization: Bearer <token>`
- `Accept: application/vnd.github+json`
- `X-GitHub-Api-Version: 2026-03-10`
- `User-Agent: weave`

Methods, each returning the parsed JSON body and the parsed `Link` header:

- `get_repo(owner, repo)` → `GET /repos/{owner}/{repo}`
- `list_issues(owner, repo, page, since)` → `GET /repos/{owner}/{repo}/issues` with `state=all`, `sort=updated`, `direction=asc`, `per_page=100`, and `since` when a watermark exists
- `get_issue(owner, repo, number)` → `GET /repos/{owner}/{repo}/issues/{number}`
- `list_comments(owner, repo, number, page, since)` → `GET /repos/{owner}/{repo}/issues/{number}/comments` with `per_page=100`

Do not add a method that reads contents, pulls files, or review comments.

Error behavior:

- `429`, and `403` when the body or headers say the rate limit is exhausted: sleep for `Retry-After` when it is present, otherwise until `x-ratelimit-reset`, then retry the same call.
- `5xx` and network failures: exponential backoff with jitter, up to 5 attempts.
- `401`, `403` that is not a rate limit, `404`, and other `4xx`: raise a typed error. Do not retry.

Pagination is the `Link` header, not a cursor in the body. The client returns the next page number, or an empty string when `rel="next"` is absent. Do not store GitHub's next URL.

Tests use recorded JSON fixtures and recorded `Link` headers. No live GitHub call.

## Phase 2. Normalize to a canonical document

Add `app/github/normalize.py`.

Use a shared timestamp helper for `updated_at`. Put it in `app/ingest/time.py`, moved from the function that already lives in `app/notion/time.py`. Notion can keep importing the moved function. The GitHub package must not import `app.notion.normalize._parse`.

Input: one issue object, or one issue-comment object plus the parent issue number.

Rules for an issue or pull request:

- `number` and `updated_at` are required. A missing either raises `ValueError`.
- Excerpt is the title, a newline, and the body, cut at 280 characters. The full title and body are hashed, then dropped.
- `kind` is `pull_request` only when `pull_request` is an object. Otherwise `kind` is `issue`.
- `status` is `merged` when `pull_request.merged_at` is a non-empty string, else `closed` when `state` is `closed`, else `open`.
- `source_url` is `html_url`.
- `version` is the sortable form of `updated_at`.

Rules for a comment:

- `id` and `updated_at` are required.
- Excerpt is the comment body cut at 280 characters. The full body is hashed, then dropped.
- `kind` is `comment`. `status` is empty. `parent_external_id` is `issue:{number}`.
- `source_url` is the comment `html_url`.

`author_external_id` is `user.login` when `user` is an object with a login. A missing user does not fail the record.

A tombstone from a delete event uses the same ids, `deleted=true`, an empty excerpt, and a hash of the empty string. The event's timestamp is the version, so it still has to be newer than the stored version to apply.

## Phase 3. Graph upsert

Extend `app/graph/upsert_document.py`. Do not add a second upsert path.

- Allow `source=github` through the existing stale-version guard. Equal version keeps the excerpt and re-merges edges. An older version does not write.
- Write `kind`, `status`, `author_external_id`, and `parent_external_id` only as query parameters.
- When `parent_external_id` is set, `MERGE` the parent issue node and `(parent)-[:CONTAINS]->(comment)`. A comment may arrive before its issue. The parent is then a placeholder with `version == ''` until the issue write fills it. Match the placeholder rule already used for Slack replies.
- A stale comment write does not create the `CONTAINS` edge. An equal version does re-merge it.

Add no new index beyond the `Document` indexes that already exist.

Tests: idempotent upsert, older version rejected, equal version edge repair, comment-before-issue placeholder, tombstone keeps `HAS_SOURCE` and `CONTAINS`.

## Phase 4. Sync state and poll worker

Add `app/github/worker.py` with `poll_repository(tenant_id, connection_id, owner, repo, client, graph)`.

On the first call, `get_repo` and keep the numeric `repo_id`. Call `write_connection()` with `source=github` and `workspace_id=repo_id`. Do not store the token on the node.

Sync node id: `{tenant}:github:sync:{repo_id}`.

Properties: `tenant_id`, `connection_id`, `workspace_id`, `owner`, `repo`, `page`, `since`, `status`, `last_error_class`, `updated_at`.

Comment pages use `{tenant}:github:sync:{repo_id}:issue:{number}` with `comments_page` and `comments_since`.

For each issues page:

1. Normalize and upsert each issue. A `ValueError` records a failure and does not move the checkpoint.
2. For each accepted issue, page its comments to the end before the issues checkpoint moves. A crash mid-comments resumes from `comments_page`.
3. Write the issues checkpoint only after those writes return. Store the next page number. When there is no next page, set `since` to the greatest `updated_at` seen, set `page` back to the first page, and mark the run `complete`.

`since` is inclusive. The next poll may see the same timestamp again. That replay is safe because the upsert is idempotent.

Do not advance `since` after a partial page.

Tests with a fake client and FalkorDB:

- Two issue pages. After page one, `since` is unchanged and `page` is the next page.
- A crash after the first comment page leaves the issues checkpoint where it was. Replay does not duplicate the issue or the comments.
- After the empty next link, a second poll does not create more `Document` nodes.
- A list payload whose `repository` points at a different repo never reaches the upsert.

## Phase 5. Failed records

`:SyncFailure` id `{tenant}:github:failure:{repo_id}:{external_id}`.

Properties: `node_id`, `version`, `error_class`, `attempts`, `updated_at`, `tenant_id`, `connection_id`, `repo_id`, `owner`, `repo`, `number`. No excerpt, no body, no token.

`retry_github_failures(graph, client)` refetches that one issue, or that one issue's comments when the failure is a comment. It does not call the issues list. Success deletes the failure node. The fifth failure stays, with `attempts` set.

A `404` for an id this connector already stored becomes a tombstone and clears the failure. A `404` for an id that was never stored stays a failure.

Filter the retry query by `source` or by the GitHub id prefix. The existing Slack query `MATCH (f:SyncFailure)` is unscoped; do not send GitHub failures through that function.

## Phase 6. Logs

One structured line per issues page and per comment page: `tenant_id`, `repo_id`, `records`, `failed`, `duration_ms`, `error_class`.

The formatter test feeds an issue title and a fake token and asserts neither string appears.

## Phase 7. One cited answer

Add `app/answer/github_question.py`.

Input: `tenant_id`, `repo_id`, and a phrase. Match `Document` nodes where `source=github`, `tenant_id` matches, `workspace_id` is that repository, `deleted` is false, and `excerpt` contains the phrase. Return excerpt, `updated_at`, `source_url`, `kind`, and `status`, newest `version` first, limit 20.

If there are no hits, say the graph has no matching GitHub issue or comment. No model call.

Test: a deleted comment is absent. A closed issue whose excerpt matches is present and includes its GitHub URL.

## Phase 8. Webhook deletes and edits

Add `POST /github/webhook` on the existing FastAPI app.

- Read the raw body and verify `X-Hub-Signature-256` with `GITHUB_WEBHOOK_SECRET` before parsing the payload as a command. Compare the HMAC with `hmac.compare_digest`. Reject a missing signature or a timestamp-free delivery that fails the HMAC. Return 401 and do not query the graph.
- Store `X-GitHub-Delivery` on a `:GithubDelivery` node, scoped by tenant, so a redelivery does not need a second write. The upsert remains safe if that check is missed.
- Accept `issues` actions `opened`, `edited`, `closed`, `reopened`, and `deleted`.
- Accept `pull_request` actions `opened`, `edited`, `closed`, and `reopened`. `closed` with `merged == true` sets `status=merged`.
- Accept `issue_comment` actions `created`, `edited`, and `deleted`.
- Ignore every other event, including `push`, `pull_request_review`, `pull_request_review_comment`, and workflow events. Log the event name only.
- Ignore a delivery whose repository id is not the configured repository.
- `deleted` goes through the normalizer as a tombstone. Edges stay.

Polls do not observe deletes. Until this phase has run, a deleted issue remains in the graph with its last excerpt. Do not mark the connector complete before this phase.

Tests: a bad signature does not query the graph. A delete blanks the excerpt and keeps `CONTAINS`. An older `edited` delivery does not replace a newer excerpt.

Document the request URL in `.env.example`. The webhook's repository events are Issues and Pull requests. Do not subscribe to Pushes.

## Environment

Add these names to `.env.example` when the code that reads them lands. Values stay empty in git.

- `GITHUB_TOKEN`
- `GITHUB_OWNER`
- `GITHUB_REPO`
- `GITHUB_CONNECTION_ID`
- `GITHUB_WEBHOOK_SECRET`
- `GITHUB_API_VERSION` defaulted in code to `2026-03-10` when the environment value is empty
- `WEAVE_TENANT_ID` reused

## Prerequisites before Phase 1

1. Leave the current Notion excerpt follow-up recorded in `BUILD.md`. This plan does not change that next step.
2. Create a fine-grained token on the GitHub account that can see the chosen repository. Permissions: Metadata read, Issues read, Pull requests read. No Contents permission.
3. Choose one repository whose issues may be stored in the demo graph.
4. Pass the Phase 0 access test. Record the numeric repository id in the operator's notes, not in git.

## Order

Phase 0 is manual. Implement Phases 1–7 in order. Phase 8 starts after Phase 3 can write a tombstone the answer query excludes.

After Phase 8, update `BUILD.md` and stop. Commits, diffs, review threads, and a cross-source answer are a separate plan.
