# Weave --- Agent Guide

> **Purpose:** This file gives AI coding agents the product context,
> architecture principles, engineering constraints, and design direction
> for building Weave. Read this before making changes. Current status is
> in `BUILD.md`.

## Current build

As of 14 October 2026 the demo has three direct connectors. Nango is not
used. The scope is a single tenant and is not permission-safe.

-   **Slack:** public channels only. Backfill, checkpoints, thread
    replies, signed `POST /slack/events`, and one cited answer are
    implemented. A live human message and a later delete were confirmed
    on the connected demo workspace. The workspace id stays in `.env`.
-   **Notion:** REST API version `2026-03-11`. Shared pages become
    `Document` nodes. One live page is stored. Its excerpt is empty
    because the page has a title and no supported block text.
-   **Google Drive:** read-only service account, Google Docs in one
    shared folder. One live Doc is stored. `requests` is required
    because `google-auth` uses it.

Full bodies stay out of the graph. Notion users and Drive owners are
not merged into Slack `Person` nodes. There is no OAuth install flow,
web UI, or model-backed answer yet.

## Shared contract: ingestion and agents (read first)

As of 15 October 2026 the work is split in two and runs in parallel.
The source of truth is `Documents/Company Brain — Architecture.pdf`.

-   **Ingestion (`app/`, connectors, extraction, resolution,
    supersede, graph writes):** owned by the connector builder.
-   **Agents (permission gate, MCP tools, answer agent, impact
    watcher):** owned by the agent builder. Built against a fake
    company graph until real ingestion output passes the validator.

Both sides import the frozen contract in `shared/`. Changes to
`shared/`, `config/weave.yaml`, or `migrations/` need both builders.
Where this section disagrees with sections 4 and 5 below, this section
wins.

### Where things are

| Piece | Location |
| --- | --- |
| Labels, edge types, properties, statuses, required fields, allowed edge endpoints | `shared/schema.py` |
| Node id builders (reproduce the existing `app/` formats) | `shared/ids.py` |
| ACL group strings | `shared/acl.py` |
| Timestamp format | `shared/timefmt.py` |
| Per-source `min_level`, `cascade_policy`, `authority`; embedding model; thresholds | `config/weave.yaml`, `shared/config.py` |
| Postgres tables: `group_membership`, `source_sync`, `tenant_acl`, `change_event`, `chunk` | `migrations/001_contract.sql` |
| Agents-side tables: `api_token` (hashed per-user tokens), `audit_log` (one row per tool call or answer), and agent memory: `conversations`, `conversation_turns`, `episodes`, `playbooks`, `tasks`. Ingestion does not write them. | `migrations/002_agent_auth.sql`, `migrations/004_audit_log.sql`, `migrations/005_agent_memory.sql` |
| MCP server (tools over HTTP, bearer token per user) and the answer API (`POST /answer`, `POST /actions/{id}/confirm`) | `agents/mcp/`, `python -m agents.mcp` |
| Answer agent (intent routing, bounded tool loop, citation validator, answer card, drafting) | `agents/answer/`, `python -m scripts.ask` |
| Agent memory (re-gated recall, conversations, task queue) | `agents/memory/` |
| Impact watcher (flow 3): claims `change_event` rows with `SKIP LOCKED`, runs `impact_of` per recipient as that recipient, queues a gated Slack DM and a customer-update draft, sends dry-run unless `watcher.send: slack` on a real tenant. Writes tasks and audit rows only. | `agents/watcher/`, `python -m agents.watcher`, `python -m scripts.flow3` |
| Contract checker | `shared/validate.py`, `python -m scripts.validate_graph` |
| Fake company (reference for what correct output looks like) | `fixtures/acme.py` |

### Stores

-   **FalkorDB Cloud** for graphs. The company graph is `co:{tenant}`,
    private graphs are `u:{tenant}:{user}`. Connection comes from
    `FALKORDB_URL` (`falkor://user:password@host:port`). Code in
    `shared/` reads it through `shared/graphdb.py`.
-   **Neon Postgres** (pgvector) for memberships, sync times, the
    ChangeEvent outbox and chunks. `DATABASE_URL` is the pooled
    endpoint for queries; `DATABASE_URL_UNPOOLED` is direct and must be
    used for migrations. `shared/pg.py` picks the right one.
-   Credentials are shared privately and live in `.env` / `.env.local`
    (both gitignored). Never commit, log or print them.
-   Tenant `acme` (graph `co:acme`) is the fake company. Do not write
    real data there. Real ingestion output goes to its own tenant, for
    example `WEAVE_TENANT=real` (graph `co:real`).

Setup check: `pip install -e .[dev] falkordb`, then
`python -m scripts.check_stores` (all `ok`), then
`python -m scripts.query_active_decision --as sam` (prints "Use
Provider B for SSO" with two citations).

### Contract rules for graph writes

1.  Never type a label, edge type, property name or status in a query.
    Import it from `shared/schema.py`.
2.  Keep the existing node ids. Build them with `shared/ids.py`;
    `tests/test_shared_contract.py` checks they equal the `app/`
    builders. People are keyed by email (`person_id`), entities by
    normalized name (`entity_id`), claims by first evidence item plus
    statement (`claim_id`).
3.  Keep source-specific labels and add the contract label next to
    them: a Slack message is `:Message:Item`, a Notion or Drive page is
    `:Document:Item`, a GitHub issue is `:Issue:Item`, a Slack channel
    is `:Conversation:Source`. Agents query only `Item` and `Source`.
4.  Keep the existing property names (`source`, `source_url`,
    `occurred_at`, `excerpt`, `content_hash`, `version`, `deleted`).
    `REQUIRED_PROPS` in `shared/schema.py` lists what each label needs.
5.  Every `Item` and `Source` carries `acl_groups` (a non-empty list
    built with `shared/acl.py`, for example `slack:C123`,
    `github:org/repo`, `notion:x`, `gdrive:x`, or `org:all`) and
    `min_level` (0 to 3). Sources also carry `cascade_policy` and
    `authority` from `config/weave.yaml`. An org-wide source (any
    employee may read it) is never just `org:all`: its items carry
    their own source group plus `org:all`, for example
    `["slack:C0ENGPUB", "org:all"]` (`shared.config.source_acl` builds
    this from `org_wide: true`). The gate hides an item when any of its
    non-`org:all` groups has no `source_sync` within 15 minutes, so a
    stale org-wide source fails closed like any other.
6.  Each `Item` has exactly one `IN_SOURCE` edge and an `AUTHORED_BY`
    edge to a `Person`.
7.  Every `Decision`, `Commitment` and `Figure` has at least one
    `EVIDENCED_BY` edge to an `Item` with `quote`, `span` (`[start,
    end]` into the item text) and `confidence` (0 to 1). No evidence,
    no fact.
8.  Claim `status` is `active`, `superseded` or `retracted`. A
    commitment's delivery state goes in `progress` (`open`, `at_risk`,
    `done`). Active claims have no `valid_to`.
9.  Supersede, never overwrite: add the new claim, add
    `(new)-[:SUPERSEDES]->(old)`, set the old claim's `status` to
    `superseded` and its `valid_to`, then insert one `change_event` row
    (`fact_id` = old id, `kind` = `superseded`, `new_fact_id`,
    `processed` = false). Event ids are `{fact_id}:{kind}`, so retries
    are safe.
10. Projections (cascaded summaries) get `status` `draft` or
    `approved`, an `audience` list of ACL strings, `level_floor` and
    `origin_id`, plus one `SUMMARIZES` edge to the origin claim.
    `broadcast` sources write `approved`; `manual` sources write
    `draft`, which a manager approves (architecture doc). Agents show
    only `approved`.
11. Timestamps come from `shared.timefmt.to_ts` (UTC, seconds
    precision). Dates without a time are `YYYY-MM-DD`.
    Dependency edges (`NEEDS`, `DEPENDS_ON`, `BLOCKED_BY`) carry
    `evidence_ids`: a non-empty list of the `Item` ids that justify the
    edge (for example the message where Sales says the deal needs SSO).
    Agents show an edge only when the caller can see at least one of
    those items and every claim at either end, so an edge without
    `evidence_ids` is never shown.
12. Writes use `MERGE` on `id` inside `UNWIND` batches so retries
    change nothing. Agents never write the company graph.
13. Group memberships go to `group_membership` with the same ACL
    strings, and every ACL sync updates `source_sync.last_good_sync_at`
    and raises `tenant_acl.acl_epoch` when anything changed.
14. Chunks go to the `chunk` table with `acl_groups` and `min_level`
    copied from the item, embedded with the model and dimension in
    `config/weave.yaml` (`BAAI/bge-small-en-v1.5`, 384).

### Ingestion to-do to reach the contract

-   Port `app/graph/upsert.py` and `app/graph/upsert_document.py` to
    add the `Item` / `Source` labels, `acl_groups`, `min_level`,
    `IN_SOURCE` and `AUTHORED_BY`, using `shared/` constants. Keep the
    existing edges until both builders agree to drop them.
-   Resolve people across Slack, GitHub and Notion into one `Person`
    keyed by email with `slack_id`, `github_login`, `notion_user`,
    `level` and `dept` (from a seeded org chart).
-   Point graph writes at `co:{tenant}` instead of the `weave` graph.
-   Add extraction of `Decision`, `Commitment` and `Figure` with
    `EVIDENCED_BY`, the supersede step with `change_event` rows, and
    projections for `broadcast` sources.
-   Move sync state (`SyncState`, `SyncFailure`, `SlackEvent`) to
    Postgres when convenient.
-   Done when `python -m scripts.validate_graph --tenant <tenant>`
    reports `errors=0` on real output. Then agents switch to that
    tenant by changing `WEAVE_TENANT`.

### Open decisions (both builders)

-   Whether `REPLIES_TO` stays in the contract.
-   Whether `AUTHORED` and `CONTAINS` are dropped once `AUTHORED_BY`
    and `IN_SOURCE` are written.

## 1. Product overview

**Weave** is a company-wide intelligence and organizational-memory
platform. It connects knowledge scattered across business tools and
helps people understand not only *what* happened, but *why*, *who was
involved*, *what changed*, and *what should happen next*.

Weave is inspired by the idea of a company brain: a persistent,
queryable map of people, teams, projects, customers, documents,
conversations, decisions, tasks, policies, and events.

The product is **not limited to engineering or technical teams**. It
should support cross-functional questions spanning Sales, Marketing,
Product, Engineering, Customer Success, HR, Finance, Operations, and
Leadership.

### Example user questions

-   "Why did we change pricing, which customers are affected, and what
    should Sales do?"
-   "What commitments did we make to this customer, and are any at
    risk?"
-   "Which decision caused this project to change direction?"
-   "What has changed since the last leadership review?"
-   "Who owns this initiative, what does it depend on, and what is
    blocking it?"
-   "Show the evidence behind this answer and link me to the original
    sources."

### Product principles

1.  **Connected context over isolated search.** Answers should connect
    evidence across tools, people, decisions, and time.
2.  **Evidence before assertion.** Ground answers in source material and
    show citations or links to supporting records.
3.  **Relationships matter.** Use FalkorDB to model and traverse
    relationships, not merely as a document bucket.
4.  **Preserve history.** Important decisions, ownership, status
    changes, and superseded information should retain temporal context.
5.  **Respect permissions.** Users must not learn information they are
    not authorized to access through search, graph traversal, summaries,
    or generated answers.
6.  **Useful across the company.** Model business concepts broadly
    rather than assuming all knowledge is code, tickets, or engineering
    discussions.
7.  **Incremental, reliable ingestion.** Syncs must be repeatable,
    observable, and safe to retry.
8.  **Clear uncertainty.** If evidence is missing or conflicting, say so
    instead of inventing an answer.

## 2. Core architecture

Use this as the intended architecture, adapting to the repository's
existing structure rather than rewriting it unnecessarily.

``` text
Business source systems
  ├── Slack / Microsoft Teams
  ├── Google Drive / Notion / SharePoint
  ├── CRM: HubSpot / Salesforce
  ├── Work management: Linear / Jira / Asana
  ├── Email / Calendar / meeting transcripts
  └── Other business systems as supported
             │
             ▼
Connector and authentication layer
  └── Nango where appropriate for OAuth, provider integrations,
      synchronization, and webhook/trigger handling
             │
             ▼
Ingestion and normalization backend
  ├── Validate and normalize provider records
  ├── Resolve identities and canonical entities
  ├── Preserve source IDs, timestamps, links, versions, and ACLs
  ├── Handle edits, deletions, retries, pagination, and checkpoints
  └── Extract entities, relationships, and candidate decisions
             │
             ▼
Knowledge and retrieval layer
  ├── FalkorDB: entities, relationships, provenance, temporal links,
  │   graph traversal, and multi-hop reasoning
  ├── Vector/full-text retrieval where supported and useful
  └── Object storage / source systems: full documents and large artifacts
             │
             ▼
Query and agent layer
  ├── Understand the question
  ├── Retrieve relevant graph paths and source evidence
  ├── Expand across connected entities when justified
  ├── Enforce permissions
  └── Produce a grounded answer with citations and uncertainty
             │
             ▼
Web application
  ├── Search and ask questions
  ├── Inspect sources and relationship paths
  ├── Explore people, projects, customers, and decisions
  └── Connect sources and manage sync status
```

### Technology direction

Use the existing repository's stack where it is already established. The
current intended direction is:

-   **Graph database:** FalkorDB, using OpenCypher-compatible queries
    for relationship storage and graph traversal.
-   **Backend:** Python with FastAPI, unless the repository already uses
    another established backend.
-   **Agent orchestration:** LangGraph or a small, explicit agent loop;
    avoid adding orchestration complexity without a clear need.
-   **LLM:** still open (Groq and Grok are candidates); keep provider
    configuration replaceable. The answer agent reads `answer.llm` in
    `config/weave.yaml`; `provider: none` runs the rule-based components,
    and a provider is one adapter class in `agents/answer/llm.py`.
-   **Frontend:** React is the intended direction. Cytoscape.js is a
    candidate for interactive graph visualization.
-   **Connector layer:** Nango where its supported integration and sync
    capabilities fit the provider and use case.
-   **Large artifacts:** Keep full documents, audio, and other large
    payloads in source systems or object storage. Store metadata, stable
    identifiers, excerpts where justified, and provenance in the graph.
-   **Additional persistence:** Use the repository's established
    relational database for operational metadata, users, tenants, sync
    state, or configuration when needed. Do not duplicate graph
    relationships there without a reason.

These are design directions, not permission to replace existing
dependencies blindly. Inspect the codebase before deciding.

## 3. FalkorDB's role

FalkorDB should be a central part of Weave's differentiator:
representing and traversing relationships across the organization.

Use it for: - Canonical entity nodes and typed relationships. -
Multi-hop queries such as decision → project → customer → commitment →
owner. - Source provenance and links from derived claims to source
records. - Temporal relationships, supersession, and change history
where needed. - Cross-functional context retrieval. - Permission-aware
traversal and filtering.

Do **not**: - Store every raw file or full conversation as an
unstructured graph property by default. - Use the graph only as a
decorative visualization layer. - Invent relationships just because two
records share a keyword. - Treat LLM-extracted relationships as verified
facts without retaining evidence and confidence. - assume a vector
search result alone establishes a relationship.

Prefer deterministic, typed edges supported by source evidence. For
inferred relationships, retain the evidence and confidence and
distinguish them from explicit source relationships.

## 4. Initial domain model

Start with a small, extensible schema. Add types only when a real
feature needs them.

### Candidate node types

-   `Tenant`
-   `Person`
-   `Team`
-   `Project`
-   `Customer`
-   `Document`
-   `Conversation`
-   `Message`
-   `Decision`
-   `Task`
-   `Goal`
-   `Policy`
-   `Event`
-   `SourceRecord` where a provider-specific canonical entity does not
    fit another type

### Candidate relationships

-   `BELONGS_TO` --- person/team/record belongs to a tenant or
    organizational unit
-   `WORKS_IN` --- person works in a team
-   `OWNS` --- person or team owns a project, task, customer, or
    initiative
-   `WORKS_ON` --- person or team works on a project
-   `RELATED_TO` --- general relation, used sparingly when a more
    specific type is unavailable
-   `MENTIONS` --- message/document mentions an entity
-   `AUTHORED` --- person authored a message or document
-   `CONTAINS` --- conversation contains messages; document contains
    sections where modeled
-   `REPLIES_TO` --- message replies to another message
-   `SUPPORTS` / `SUPPORTED_BY` --- evidence supports a decision or
    claim
-   `DECIDED` --- person/team made a decision
-   `AFFECTS` --- decision or event affects a project, customer, team,
    or policy
-   `DEPENDS_ON` --- project/task depends on another entity
-   `SUPERSEDES` --- newer decision or record supersedes an older one
-   `HAS_SOURCE` --- canonical entity or claim links to source
    provenance
-   `HAS_CONNECTION` --- tenant has a connected provider account
-   `HAS_CONVERSATION` --- workspace/channel is associated with a
    conversation

Use consistent directionality and document it. Avoid creating inverse
edges automatically unless there is a concrete query or performance
reason.

### Common properties

Where applicable, preserve: - `tenant_id` - `source` - `connection_id` -
`workspace_id` or provider account ID - `external_id` - `object_type` -
`created_at` - `updated_at` - `occurred_at` for event time -
`ingested_at` - `source_url` - `version` or source revision - `deleted`
/ tombstone state - `visibility` / ACL metadata - `metadata` for
provider-specific fields that are not part of the canonical schema

Use stable IDs and deterministic upserts so the same provider record
does not create duplicate nodes on every sync. Tenant-scope all records
and queries.

## 5. Source integration strategy

Build a narrow, reliable end-to-end vertical slice before adding many
integrations.

### Initial connector priority

A useful cross-functional MVP can start with: 1. **Slack** for
conversations, people, decisions, commitments, and context. 2. **Notion
or Google Drive** for durable documents, plans, and policies. 3.
**HubSpot** for customer and sales context. 4. **Linear** for projects,
issues, and delivery status.

Select the first one based on the current milestone and repository
state. Do not implement all four before proving the ingestion-to-answer
path.

### Nango guidance

When using Nango: - Verify the current provider integration and its
exact supported capabilities in the official documentation or
integration templates. - Use Nango for authentication and provider
API/sync workflows where appropriate. - A sync completion event does not
by itself mean the records are already in FalkorDB. The backend must
retrieve or receive the synced records, normalize them, and explicitly
upsert the graph. - `batchSave()` writes to Nango sync storage; it does
not directly write to FalkorDB. - Use webhooks/triggers and incremental
sync for ongoing ingestion where supported. - Use MCP for on-demand live
reads or actions where useful. MCP is complementary to persistent
ingestion; it is not, by itself, a maintained historical company
graph. - Implement provider-specific pagination, incremental cursors,
rate limits, retries, and deletion/edit semantics. - Never expose OAuth
tokens or secrets to the browser, logs, model prompts, or source
control.

### Canonical ingestion record

Normalize provider objects into a canonical envelope before graph
writes. Fields vary by object type, but aim to preserve:

``` json
{
  "tenant_id": "tenant-id",
  "connection_id": "connection-id",
  "source": "slack",
  "workspace_id": "provider-workspace-id",
  "object_type": "message",
  "external_id": "provider-object-id",
  "parent_id": "optional-parent-id",
  "author_id": "optional-provider-user-id",
  "content": "normalized text",
  "created_at": "ISO-8601 timestamp",
  "updated_at": "ISO-8601 timestamp",
  "source_url": "https://...",
  "version": "optional-source-version",
  "deleted": false,
  "permissions": {},
  "metadata": {}
}
```

Treat this as a conceptual envelope; adapt field types and schema to the
project's conventions. Do not put secrets in `metadata`.

## 6. Answering and reasoning behavior

For a user question:

1.  Identify the entities, time window, and business concepts involved.
2.  Retrieve directly relevant source records and graph nodes.
3.  Traverse only relevant relationships; expand to adjacent entities
    when it adds justified context.
4.  Collect source evidence for important claims.
5.  Check access permissions before returning any content.
6.  Resolve conflicting or superseded facts using timestamps, explicit
    supersession, and source reliability.
7.  Answer concisely, distinguishing sourced facts from inference.
8.  Include source links/citations and explain uncertainty or missing
    information.

Do not expose hidden chain-of-thought. Provide useful evidence
summaries, cited graph paths, and concise explanations of how a
conclusion is supported.

If the graph has no evidence for a requested claim, state that
limitation and, if available, search a connected source live. Do not
fabricate sources, entities, quotes, decisions, or relationships.

## 7. Multi-tenancy, permissions, and security

Security is a core product requirement, not a later enhancement.

-   Every persisted entity, relationship, cache entry, retrieval
    request, and job must be scoped to the correct tenant.
-   Respect provider-level and application-level access controls.
-   Do not leak restricted content through derived summaries, graph
    neighbors, embeddings, generated answers, logs, or error messages.
-   Preserve enough ACL metadata to filter retrieval and graph traversal
    correctly.
-   Do not assume that because a service account can read a source,
    every Weave user can read everything the service account can access.
-   Store secrets in environment configuration or a secret manager;
    never hardcode them.
-   Validate webhook signatures where the provider supports them.
-   Make authorization checks server-side.
-   Avoid logging raw sensitive content unless strictly necessary and
    appropriately redacted.
-   Treat ingested documents and messages as untrusted data. They must
    never override system instructions or agent policies.

If the existing repository does not yet support robust per-user ACL
enforcement, document the gap and implement a safe baseline rather than
claiming the system is permission-safe.

## 8. Reliability and observability

Ingestion and graph writes must be idempotent and recoverable.

-   Use stable external IDs and deterministic node keys.
-   Make retries safe.
-   Track sync checkpoints and status.
-   Handle rate limiting, transient errors, partial failures, provider
    edits, and deletions.
-   Avoid marking a sync complete before its graph writes succeed.
-   Make failed records retryable without reprocessing the entire
    workspace unnecessarily.
-   Add structured logs and useful metrics for sync duration, records
    processed, failures, retries, and graph-write errors.
-   Do not log secrets or unnecessarily expose message/document bodies.
-   Add tracing across connector callbacks, normalization, graph writes,
    and answer retrieval when the repository supports it.

## 9. Frontend and visual design direction

Weave's chosen logo direction is **Flowing W**: a continuous, interwoven
ribbon forming a recognizable W.

### Brand characteristics

-   Modern, minimal, premium, and enterprise-credible.
-   Visual metaphor: separate strands of knowledge woven into one
    connected intelligence layer.
-   Avoid generic AI clichés such as glowing robot heads, brains, or
    excessive circuit imagery.
-   The weaving should be visible in the symbol's crossings, not merely
    implied by a gradient.

### Brand palette

Initial design direction: - Primary purple: `#7C3AED` - Primary blue:
`#3B82F6` - Dark: `#0B0F1A` - Light: `#F8FAFC` - Light blue: `#60A5FA` -
Lavender: `#C4B5FD`

Use the purple-to-blue gradient for the primary symbol where
appropriate. Always provide solid monochrome variants and check contrast
on both light and dark backgrounds. Treat these as starting design
tokens; validate them against actual accessibility and UI needs.

### Logo usage

-   Primary: ribbon W symbol plus "Weave" wordmark.
-   Light surfaces: gradient symbol with dark wordmark.
-   Dark surfaces: gradient symbol with white wordmark.
-   App icon: symbol alone, centered in a rounded square.
-   Monochrome: solid black or white where gradients are unsuitable.
-   Keep the mark legible at small sizes and preserve clear space.
-   The generated brand board is a concept reference, not
    production-ready vector artwork. If the repository includes a logo
    asset, inspect it and use the canonical asset. Do not recreate a
    close imitation in CSS or use a raster concept board as the logo. If
    no vector asset exists, document the need for a clean SVG master.

### Product UI

-   Prefer clean hierarchy, generous spacing, restrained color, and
    readable typography.
-   Prioritize search, grounded answers, citations, source previews,
    relationship exploration, and sync status.
-   Graph visualizations should answer a user question, not serve as
    decoration.
-   Keep node labels, edge labels, filters, and provenance accessible
    and understandable.
-   Ensure useful empty, loading, error, and permission-denied states.
-   Do not invent unsupported product capabilities in the UI.

## 10. Engineering workflow for coding agents

Before making changes: 1. Inspect the repository, `README`,
package/dependency files, existing architecture, environment examples,
and tests. 2. Identify the current implementation and report assumptions
or missing context. 3. Reuse established patterns and dependencies
wherever reasonable. 4. Propose a concise implementation plan for
substantial changes. 5. Make the smallest complete vertical slice that
satisfies the requirement.

While implementing: - Keep modules cohesive and avoid unnecessary
abstractions. - Add or update tests for meaningful behavior. - Use type
hints and input validation where supported by the stack. - Handle errors
explicitly; avoid broad silent exception handling. - Never commit
credentials, tokens, real customer data, or local environment files. -
Add configuration to the documented environment template without adding
real secret values. - Keep API contracts and graph schema documented. -
Avoid destructive schema changes without a migration or compatibility
plan.

Before finishing: 1. Run the relevant tests, linters, and type checks
available in the repository. 2. Review the diff for secrets, accidental
scope changes, duplicate logic, and unhandled failure cases. 3.
Summarize what changed, the commands/tests run, known limitations, and
any required configuration. 4. Do not claim tests passed unless they
were actually run.

## 11. MVP acceptance criteria

A meaningful first vertical slice should demonstrate:

-   A user can securely connect at least one supported source.
-   Initial backfill and incremental updates work, with safe retries.
-   Records are normalized and upserted into FalkorDB with stable IDs
    and tenant scope.
-   Core entities and relationships can be traversed using graph
    queries.
-   A user can ask a cross-record question and receive a grounded answer
    with source links.
-   The system respects source permissions or clearly limits access to a
    documented safe demo scope.
-   Edits and deletions are reflected correctly.
-   Sync failures are observable and recoverable.
-   Tests cover normalization, idempotency, graph writes, retrieval, and
    important authorization boundaries.

Do not count a static mockup, a disconnected chatbot, or a graph
visualization with hardcoded nodes as a completed company-brain
implementation.

## 12. Decision-making rule

When requirements conflict, prioritize in this order:

1.  Security, tenant isolation, and permissions.
2.  Correctness, evidence, and provenance.
3.  Reliable ingestion and recoverability.
4.  Useful cross-functional graph reasoning.
5.  Clear product experience.
6.  Performance and cost optimization.
7.  Additional integrations and visual polish.

**North star:** Weave should help every team understand how people,
information, decisions, and work connect---while making each important
answer traceable to its evidence.
