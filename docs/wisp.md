# Wisp — Cursor companion execution plan

**As of 16 October 2026.** Wisp is Weave’s small animated companion: cute, friendly, and grounded in the company graph. This document is the execution plan. It does not mean Wisp is shipped yet.

Locked choices for the first version:

| Choice | Decision |
| --- | --- |
| Name | **Wisp** |
| Surface | **Cursor first** — chat (MCP) plus a status-bar / side-panel pet |
| Screen cue | **Open file** in the active editor (path + bounded excerpt) |
| When Wisp speaks | When you **ask**, and when the **watcher** has a notice for you |
| Graph | **`co:real`** as the viewer (`WEAVE_VIEWER_EMAIL` person), same bearer token as the agent |

Wisp does not float over Gmail or Claude Code in v1. The desktop pet below is a local Windows companion. It does not call the agent. See [deployment.md](deployment.md) for the agent server.

## Desktop companion

The pet is `cursor-wisp/desktop/pet.py`. It starts as classic Wisp, the round purple one. On first launch it appears beside the mouse. After that it stays near the **text caret** while you type, when Windows reports one, and it does not follow the pointer around the screen. It notices that keys were pressed. It does not read or store the characters.

The eyes look at the caret when Windows provides one. Editors that do not expose a caret, including Cursor, give the eyes the spot where the keys are being pressed instead. Dragging the pet away pins it there. It keeps facing that spot: the body leans toward it and the eyes look that way. While it is idle the eyes droop, still aimed to the side. Typing makes the lean stronger. It does not walk back. **Ctrl+Alt+.** clears the pin, jumps to the mouse, and the next typing session follows the caret again.

The pet stays above ordinary windows. A maximized window does not hide it, and neither does clicking the desktop. A real fullscreen app that covers the whole monitor, including the taskbar, hides it until that app leaves fullscreen.

One pet can run at a time. A second start exits immediately.

### Before the first start

The window class is compiled once, next to `pet.py`. From `cursor-wisp/desktop`:

```text
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:library /r:System.Windows.Forms.dll /r:System.Drawing.dll /r:%APPDATA%\Python\Python312\site-packages\webview\lib\Microsoft.Web.WebView2.WinForms.dll /r:%APPDATA%\Python\Python312\site-packages\webview\lib\Microsoft.Web.WebView2.Core.dll /out:PetForm.dll PetForm.cs
```

That needs the WebView2 runtime and `pywebview` installed for the current user. `PetForm.dll` is a local build output and is not in git.

The caret helper is compiled once the same way:

```text
C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe /nologo /target:library /r:%WINDIR%\Microsoft.NET\assembly\GAC_MSIL\UIAutomationTypes\v4.0_4.0.0.0__31bf3856ad364e35\UIAutomationTypes.dll /r:%WINDIR%\Microsoft.NET\assembly\GAC_MSIL\UIAutomationClient\v4.0_4.0.0.0__31bf3856ad364e35\UIAutomationClient.dll /r:%WINDIR%\Microsoft.NET\assembly\GAC_MSIL\WindowsBase\v4.0_4.0.0.0__31bf3856ad364e35\WindowsBase.dll /out:WispCaret.dll WispCaret.cs
```

`WispCaret.dll` is also a local build output and is not in git.

### Start

From `cursor-wisp/desktop`:

```text
pythonw pet.py
```

`python pet.py` does the same and keeps a console open. The character appears beside the mouse.

### Stop

Press **Ctrl+Alt+W**.

If the window is gone but a later start does nothing, the previous process is still holding the lock. End the `pythonw.exe` or `python.exe` whose command line contains `pet.py`, then start it again.

### Controls

| Action | What happens |
| --- | --- |
| **Left-click and drag** anywhere on the pet | Move it and pin it. It stays put, with sad eyes while idle, until you recall it. A left click does not hide it |
| **Type while it is pinned away** | It turns and peeks toward the caret, or toward where the keys are pressed when no caret is available. It does not walk back |
| **Ctrl+Alt+.** (period), or the numpad decimal | Jump to the mouse, clear the pin, and follow the caret again on the next typing |
| **Right-click** the body | Eyes close for a moment and the body nudges |
| **Double right-click** the body | Next companion: Wisp, Knot, Loaf, Pebble, Spark, Spindle |
| **Middle-click** the pet | Hide it while the button is held, so the click can pass through |
| **Ctrl+Alt+W** | Closes the pet |

The cast is also previewed at `frontend/wisp-variants.html`. Classic Wisp on the ask page is `frontend/wisp.svg`.

## What Wisp is allowed to know

The open file is only a **search cue**. Suggestions must come from ingested items in `co:real` (Slack, Notion, Drive, GitHub excerpts in `chunk` and the graph), with the same permission gate as `search_context` and `POST /answer`.

Wisp must not invent facts from the file text, store full file bodies in Postgres, or send secrets from disk.

## Character

Wisp is a small woven-ribbon creature in Weave purple (`#7C3AED`) to blue (`#3B82F6`), with two soft eyes, a lavender antenna, two little feet, and a **ribbon tail**.

| State | Look | Motion |
| --- | --- | --- |
| **Resting** | Slow blink, tail loose | Gentle **float** (±5px), slow **sway** (±2°), alternating foot bob |
| **Reading** | Eyes open, antenna forward | Faster float and sway while the cue request is in flight |
| **Found** | Tail ties a small knot | One extra float cycle, then settle; knot visible via `data-state="found"` |
| **Unsure** | Head tilt, tail loose | Slower sway with a slight tilt (~6°) |
| **Alert** | Antenna bulb glow | Same float as resting; lavender glow on the bulb until dismissed |

**Voice:** one short sentence, then quote, source label, and link. If there is no evidence, say so plainly.

**Art:** shared assets under [frontend/](../frontend/). The ask page and the extension default to [frontend/wisp.svg](../frontend/wisp.svg) (classic). Other bodies live in [frontend/wisp/](../frontend/wisp/) and are compared on [frontend/wisp-variants.html](../frontend/wisp-variants.html). Each file is a shaded ribbon (highlight, shadow, catchlights) with its own float. Set `data-state` on the root `.wisp` group of the classic file.

The five companions are static SVG drawings in [frontend/wisp/](../frontend/wisp/), compared on [frontend/wisp-variants.html](../frontend/wisp-variants.html). Classic Weave ribbon stays [frontend/wisp.svg](../frontend/wisp.svg) for the ask page.

| Variant | File | Identity |
| --- | --- | --- |
| Knot | `frontend/wisp/knot.svg` | Teal curl, lilac wrap, shy warm face |
| Loaf | `frontend/wisp/loaf.svg` | Wide honey body, heavy lids, cream belly |
| Pebble | `frontend/wisp/pebble.svg` | Moss stone, amber eyes, one small bud |
| Spark | `frontend/wisp/spark.svg` | Coral teardrop, wink, golden line |
| Spindle | `frontend/wisp/spindle.svg` | Tall indigo, upward gaze, one cyan mark |

### Motion spec (page + extension)

Wisp should always feel **alive**: it floats in place rather than sitting on a baseline.

| Layer | Resting | Panel / hero (optional) |
| --- | --- | --- |
| **Idle float** | `wisp-float` 3.2s ease-in-out, ±5px Y | Parent `.pet` **wander** drifts ±22px X / ±16px Y over ~6.5s |
| **Sway** | `wisp-sway` 5s, ±2° | Combined with wander for a lazy orbit |
| **Blink** | `wisp-blink` 4.5s | Same in all states except reduced-motion |
| **Feet** | Alternating `wisp-feet` | Hidden or static in 16×16 status bar |

Implementation notes:

- Set `data-state` to `resting` \| `reading` \| `found` \| `unsure` \| `alert` on the SVG root `.wisp` element (extension can reach into the object document when same-origin).
- Respect `prefers-reduced-motion: reduce`: disable float, sway, blink, and foot animations (defined in the SVG stylesheet).
- Status bar icon: use a static frame or only the inner float (no wander) so the glyph stays readable.
- **Listening** on the ask page maps to `reading`; typing in the question box maps to `resting` with wander disabled and a small translate toward the input.

## Architecture

```mermaid
flowchart LR
  editor[Cursor editor]
  ext[Wisp extension]
  chat[Cursor chat]
  cue["POST /wisp/cue"]
  notices["GET /notices"]
  mcp["MCP /mcp"]
  graph["co:real"]
  tasks[tasks]
  editor --> ext
  ext --> cue
  ext --> notices
  chat --> mcp
  cue --> graph
  mcp --> graph
  tasks --> notices
```

Two clients share one **viewer bearer token** and the **agent server** (`python -m agents.mcp`, port **8765** locally):

1. **Cursor chat** — MCP at `/mcp` (`search_context`, `get_decision`, …).
2. **Wisp extension** — `POST /wisp/cue` on file change; `GET /notices` on a timer for watcher alerts.

Do not point Wisp at the webhook app (port 8000). Claim routes there are not bearer-protected yet.

Environment (never commit values):

- `WEAVE_AGENT_URL` — e.g. `http://127.0.0.1:8765`
- `WEAVE_VIEWER_TOKEN` — existing `api_token` for tenant `real` and the viewer person

## Gaps, caveats, and how to close them

Each gap is closed in the phase listed. Do not start a later phase until the close for its dependencies is in place.

### G1 — File text would be stored as a “question”

**Problem:** [agents/answer/agent.py](../agents/answer/agent.py) writes the question into `episodes` and `conversation_turns`. Sending an entire source file through `POST /answer` would persist code in Postgres.

**Close (phase 2):** Add `POST /wisp/cue` on the agent app ([agents/mcp/server.py](../agents/mcp/server.py)):

- Body: `{ "path": string, "excerpt": string }` (excerpt required, max 1500 chars).
- Run gated `search_context` (and optional keyword extraction from identifiers / quoted strings in the excerpt — no LLM; `answer.llm.provider` is `none`).
- Response: `{ "state": "found" | "unsure", "summary": string, "citations": [...] }` in the same shape as the answer card’s public citations.
- Persist at most: path, citation item ids, and timestamp in a small `wisp_cue` audit row or omit persistence entirely for v1.

### G2 — Question length cap

**Problem:** `MAX_QUESTION_CHARS` is 2000 in [agents/mcp/server.py](../agents/mcp/server.py).

**Close (phase 5):** Extension sends path plus ≤1500 characters from the visible editor range (or whole file if smaller). Never attach the bearer token to the body.

### G3 — Rule-based agent does not “understand” code

**Problem:** With `provider: none`, keyword intent routing will not reason about arbitrary source files.

**Close (phase 2):** `/wisp/cue` does not call the full answer loop. It searches the graph on excerpt tokens and returns Found only when `search_context` returns gated hits. Unsure is a valid success state.

### G4 — Watcher “eats” Slack DM tasks

**Problem:** [agents/watcher/sender.py](../agents/watcher/sender.py) claims `kind=slack_dm` tasks and moves them to `sent`, including dry-run delivery. A pet that only polls `state=ready` will miss notices.

**Close (phase 4):** `GET /notices` (bearer, web surface):

- Select `tasks` where `tenant_id` matches, `payload->>'recipient'` is the caller’s `person_id`, and `state` in (`ready`, `sent`).
- Do **not** claim or change state on read.
- `POST /notices/{id}/seen` sets `payload.wisp_seen_at` (ISO timestamp) without blocking Slack send.
- Return only tasks where `wisp_seen_at` is absent and evidence node ids still pass the gate.

### G5 — No live watcher alerts on `co:real` today

**Problem:** `co:real` has excerpts and people, but no commitments, deals, or `change_event` rows. `impact_of` will not notify the viewer yet.

**Close (phase 6):**

- **Phase 6a:** Prove **Found** via `/wisp/cue` or MCP search on text that matches the Drive test doc and the Slack events message (same as `scripts.ask --local`).
- **Phase 6b:** Prove **Alert** in CI by inserting one fixture-shaped `tasks` row for the viewer and asserting `GET /notices` returns it; do not require a live supersede on `co:real`.
- A dark antenna in production is **not** a failed deploy until claims and watcher events exist on `real`.

### G6 — Secrets on screen

**Problem:** Opening `.env` or `secrets/gdrive-service-account.json` in Cursor could be sent as a cue.

**Close (phase 5):** Extension blocklist (case-insensitive path segments):

- `.env`, `.env.local`, `.env.tokens`
- `secrets/`, `credentials/`
- Files ending in `.pem`, `.key`, `service-account.json`

If blocked, show Resting and do not call the server.

### G7 — Untrusted file text (prompt injection)

**Problem:** A file could contain “ignore instructions” text.

**Close (phase 2):** Excerpt is search input only; it is not merged into system prompts. No LLM adapter runs on `/wisp/cue` while `provider: none`.

### G8 — Cursor rules are not the pet

**Problem:** A project rule cannot debounce editor changes or paint a status icon.

**Close:** Phase 3 = MCP + rule for **chat**. Phase 5 = **extension** for the pet, file cue, and notice polling.

### G9 — Token handling

**Close (phase 3):** Add [.cursor/mcp.json](../.cursor/mcp.json) (or documented template) that references env vars, not literal tokens. Document extension settings mirroring the same vars. Add `docs/wisp.md` to `.gitignore` only if you copy tokens into a local notes file — do not gitignore this plan.

### G10 — Wrong server / open claim routes

**Close:** Wisp uses the agent paths on the public origin. `POST /claims/*` requires the viewer bearer token and returns 401 without it (see [deployment.md](deployment.md)).

### G11 — No shared mascot asset

**Close (phase 1):** Add `frontend/wisp.svg` and optionally restyle the ask-screen pet in [frontend/index.html](../frontend/index.html) to use it.

## Execution phases

### Phase 1 — Character sheet and SVG

**Deliverables**

- [frontend/wisp.svg](../frontend/wisp.svg) with `data-state` tail knot, float, sway, blink, foot bob, and alert glow.
- [frontend/index.html](../frontend/index.html) loads the SVG via `<object>` and applies hero **wander** on the wrapper.
- Motion table in this doc (above).

**Exit:** Asset renders at status-bar scale; opening `frontend/index.html` shows Wisp floating and wandering; `prefers-reduced-motion` freezes animation.

### Phase 2 — `POST /wisp/cue`

**Deliverables**

- Route on agent app; bearer via existing [BearerAuth](../agents/mcp/server.py).
- Module e.g. `agents/wisp/cue.py` calling `search_context` with a query built from path + excerpt.
- Tests with MemoryStore / fixture graph: found when chunk matches; unsure when not; rejects overlong excerpt.

**Exit:** `pytest` for cue tests green; manual curl with viewer token against local `8765`.

### Phase 3 — Cursor MCP and chat rule

**Deliverables**

- `.cursor/mcp.json` template pointing at `WEAVE_AGENT_URL` + token env.
- Project rule (e.g. `.cursor/rules/weave.mdc`): for decisions, dates, owners, and customers, use Weave MCP tools; cite sources; do not guess when search returns empty.

**Exit:** In Cursor chat, “What does the Weave test document say?” returns citations from `co:real` (agent deployed or local).

### Phase 4 — `GET /notices` and dismiss

**Deliverables**

- `GET /notices` and `POST /notices/{id}/seen` on agent app.
- Filter by `payload.recipient`; re-gate `node_ids`; hide stale evidence.
- Tests: unread notice returned; after seen, omitted; sender can still claim DM task.

**Exit:** Tests green; no change to watcher claim order.

### Phase 5 — Cursor extension (minimal)

**Deliverables**

- `wisp/` or `cursor-wisp/` extension: status bar icon, webview panel, debounced active-editor listener (~2s), path blocklist, cue POST, notice poll (~60s).
- Map response to Resting / Reading / Found / Unsure / Alert via `data-state` on [frontend/wisp.svg](../frontend/wisp.svg).
- Webview panel: same SVG at ~96px with inner float only; optional subtle CSS translate on state change.

**Exit:** Opening a file whose text matches ingested excerpts shows Found with link and tail knot; unrelated file shows Unsure; blocked paths never POST. Pet visibly floats whenever the panel is open.

### Phase 6 — Proof and documentation

**Deliverables**

- Checklist in this file (below) executed and dates filled in.
- Optional: link from [BUILD.md](../BUILD.md) to this doc.

**Exit:** Phase 6a search proof on `real`; phase 6b notice test in CI.

## Proof checklist

Run with `WEAVE_TENANT=real`, agent on `8765`, viewer token set.

| Step | Command / action | Expected |
| --- | --- | --- |
| Health | `curl -s http://127.0.0.1:8765/health` | `ok` |
| Search (chat) | MCP or `python -m scripts.ask --local "Weave test document"` | Citations to Drive doc and/or Slack events message |
| Cue (API) | `POST /wisp/cue` with excerpt containing “Weave test doc” | `state: found`, ≥1 citation |
| Cue empty | Excerpt with random UUIDs | `state: unsure` |
| Notices (live) | `GET /notices` before any supersede | `[]` or only test rows — **acceptable** |
| Notices (CI) | Insert test task with `recipient` = viewer person id | Alert state in extension test harness |
| Secrets | Open `.env` in editor | Extension does not POST |

Record run date: _______________

## Out of scope for v1

- Floating window over Gmail, Outlook, or Claude Code.
- Reading email bodies from the screen (email is not a connector).
- Writing the company graph from the pet.
- `answer.llm.provider` other than `none` for `/wisp/cue` (revisit if a small model is enabled later).
- Authenticating claim routes on the webhook app (separate security task).

## Related docs

- [ingestion.md](ingestion.md) — what is in `co:real` today.
- [deployment.md](deployment.md) — hosting the agent server for Cursor.
- [AGENTS.md](../AGENTS.md) — contract and gate rules Wisp must respect.
