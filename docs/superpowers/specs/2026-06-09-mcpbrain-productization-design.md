# Productize mcpbrain for non-technical, multi-user rollout — design

**Date:** 2026-06-09
**Status:** approved (brainstorm), pending spec review
**Owner:** Josh Kemp

## Goal

Turn mcpbrain from a clone-and-run developer tool into an **app a non-technical
colleague can install in one line and trust** — where each user runs the app
under *their own* identity and data, the install needs no terminal beyond a
single copy-paste line, updates happen on their own, and every connection
reports its *real* state rather than a static instruction.

Three constraints frame every decision below:

1. **$0 budget.** No paid code signing / notarization, no Google OAuth public
   verification (CASA), no paid hosting. This is a hard limit, and it *selects*
   the architecture (the package-manager channel) rather than merely trimming it.
2. **Source stays private; no per-user accounts.** Distribution must not require
   each user to hold a GitHub or other account, and must not publish the source.
3. **Non-technical audience.** After one bootstrap line, the entire experience is
   browser + menu bar. No logs, no terminal, no manual update command.

The work splits into four parts that ship as one coordinated effort because
they are interdependent (the distribution model changes how `update` works; the
multi-user gate is a UI state; the UX renders off the same status layer; backfill
progress and the enrichment gate surface in that same status layer):

- **Part 1 — Multi-user readiness** (correctness): stop the app silently being
  "Josh".
- **Part 2 — Distribution & release** (the "app" delivery): published versioned
  wheels, one-line install, silent auto-update.
- **Part 3 — UX & experience**: a state-aware home, verified live status for
  every connection, a glanceable menu bar, and self-healing.
- **Part 4 — Backfill**: index all history newest-first (largely built), and a
  one-shot enrichment backfill over the backlog using the local Claude Code CLI.

## Background: product-grade engine, developer-grade shell

The engine is already product-grade — per-user `app_dir()` data isolation,
per-OS login agents, a browser onboarding wizard, atomic 0600 config writes,
dynamic loopback ports, a working `update` path. The problem is the *shell*
around it, which the README states plainly: "an unsigned, clone-and-run tool
shared from a private repo." The four load-bearing gaps:

1. It requires git + a terminal + a **permanent source checkout** (`mcpbrain
   update` does `git pull` on the clone, so every user keeps the repo forever).
2. It **builds from source on every machine** (compiled deps compile per-install).
3. It is **unsigned/unnotarized** (closeable only with money — deferred).
4. There is **no real release channel** (version pinned `0.1.0`; "update" = branch
   HEAD; a bad commit reaches everyone with no staging or rollback).

Parts 1–3 close gaps 1, 2, 4 and the multi-user/UX gaps at $0. Gap 3 and the
OAuth user cap are explicitly deferred (they cost money).

## Explicitly out of scope (and why)

- **Code signing / notarization / native double-click installers.** Apple
  Developer ID + notarization (~$99/yr) and a Windows code-signing cert
  (~$100–400/yr) are the only way to ship a double-click installer that
  Gatekeeper/SmartScreen won't block. At $0 these are off the table. The
  package-manager channel (Part 2) sidesteps Gatekeeper/SmartScreen entirely —
  code installed by `uv` is not a downloaded app bundle and never gets the
  quarantine flag — so we get a trustworthy install without signing.
- **Google OAuth public verification + CASA assessment.** Recurring real cost.
  Stays in Testing/unverified with the ~100 test-user cap and the
  "unverified app → Advanced → Continue" screen. Fine for one org; the wizard
  hand-holds through the warning (Part 3).
- **Public PyPI.** The build is made PyPI-ready (same wheels), but publishing
  publicly would expose the source-equivalent package and invite strangers to
  the Google consent screen for no benefit. Deferred to a one-step flip later.
- **Genuinely access-controlled private source distribution.** Real access
  control needs accounts or paid hosting. Out of scope at $0; see Part 2 for why
  this costs almost nothing in practice.

---

## Part 1 — Multi-user readiness (correctness)

### Principle: an unconfigured install is a *blank* brain, never Josh's

Today `config.py` centralizes identity but every helper falls back to Josh's
real value (`owner_email()` → `josh.k@centrepoint.church`, `owner_name()` →
`"Josh"`, etc.), and `orgs.py` defaults to the Centrepoint/ACC taxonomy. So a
new user who skips onboarding gets a daemon that detects *Josh* as the
self-email, attributes entities to Josh, and classifies mail into Josh's orgs.
The fix is twofold: **gate** (refuse to enrich until configured) and
**neutralize** (no Josh-shaped fallbacks remain).

### 1.1 Fail-loud identity/org gate

Add an explicit "install configured?" check. The daemon's enrichment/extraction
path **must not run** until both are true:

- identity is set: `owner_name` **and** `owner_email` present in config;
- at least one org (with a non-blank name; domains optional) is present in config.

When the gate is unsatisfied, the daemon does normal *sync* (mail/doc ingest is
identity-agnostic) but **skips enrichment/graph extraction**. This is the single
source of truth the UI renders as the "Finish setup" state (Part 3).

**Implementation (Plan 1):** the predicate is `config.is_configured(home)`, which
reads the raw `orgs` config key directly (not `orgs.taxonomy_from_config`) to
avoid an import cycle (orgs imports config). The gate itself is
`daemon._gated_enrich_mode(mode, home)` called in `run_one`: it forces the
per-cycle `enrich_mode` to `"off"` unless `is_configured`, so sync still runs.
The existing `enrich_mode` default of `"off"` remains the base lever. **Surfacing
`configured` in `daemon.status()`** (for the UI "Finish setup" state) is built
with the status/probe layer in **Plan 3**, not Plan 1; Plan 1 covers the gate
behaviour, fully unit-tested via `is_configured` and `_gated_enrich_mode`.

Rationale for gating enrichment specifically (not all sync): enrichment is where
owner identity and org taxonomy are *written into* the graph. Sync without
enrichment produces no mis-attributed records, so a partially-onboarded user
still gets their mail indexed for plain search without corrupting the graph.

### 1.2 Neutralize the Josh-shaped defaults

- `config.owner_name/owner_full_name/owner_role/owner_email`: defaults become the
  **empty string**, not Josh's values. `owner_aliases` derives only from the
  configured name/full-name (the hardcoded `"joshua"` special case is removed). With
  the gate in 1.1 the daemon never reaches enrichment with empty identity, so there
  is no behavioural default to preserve. (Keep the helpers and config keys; only the
  fallback literals change.)
- `orgs.DEFAULT_TAXONOMY`: becomes **empty**; `orgs.taxonomy_from_config(home)`
  is the only source of orgs. The gate ensures ≥1 org before enrichment.

### 1.3 Kill the literal-"Josh" bypasses

Route these through the config helpers instead of hardcoded literals:

- `draft.py:150` — `"Write an email reply from Josh Kemp."` →
  `config.owner_full_name(home)`.
- `mcp_server.py:641` — `owner=arguments.get("owner", "Josh")` →
  `arguments.get("owner") or config.owner_name(home)`.
- `joshbrain_write.py:46` — `owner: str = "Josh"` → resolve from
  `config.owner_name(home)` (caller passes it; no Josh default).
- `clickup_sync.py:141` — `owner="Joshua"` → `config.owner_name(home)`.

### 1.4 ClickUp per-user config

Add three helpers to `config.py` and route `clickup.py` through them (no Josh
defaults):

- `clickup_user_id(home)` — replaces `clickup.py:48` `_OWNER_ASSIGNEE = 72748441`.
- `clickup_list_id(home)` — replaces the hardcoded "Josh Kemp To do" list at
  `clickup.py:29` (helper already exists in config; ensure clickup.py uses it).
- `clickup_org_field_id(home)` — replaces `clickup.py:34` `ORG_FIELD_ID`.

`clickup_user_id` returns `int | None` (unassigned when unset, rather than a wrong
default). `create_task` and `_normalise_task` read these per `home`.

The wizard collects these (it already collects `clickup_api_key`).

**Deferred follow-up:** `clickup.py:_ORG_OPTION_IDS` (the brain-org → ClickUp
dropdown *option-id* map) is also install-specific. It only affects *writing* the
Org custom field and degrades to "no org tag" when unmatched, so making it
config-driven is out of scope for Plan 1 and tracked as a follow-up.

### 1.5 Records repo → per-user local git in app-dir, renamed

The "joshbrain" repo is the daemon's structured-records store (decisions, notes,
memories, continuity, voice, scaffolding). Its entire write path is git-based
(`joshbrain_write.py` commits by name; the prune/gardener cadences commit), so
it must remain a git repo — but it does **not** need to be a separate *product*
repo with a remote, and it must not be named after Josh.

- **Location:** a per-user **local git repo inside app-dir**, default
  `<home>/records` (`home` = app-dir). No remote, no clone, no shared repo.
  Per-user by construction; never pushed; already inside `backup.py`'s snapshot
  scope.
- **Config (Plan 2):** new `config.records_dir(home)` resolves `records_dir` key →
  **legacy `joshbrain_dir` key (back-compat for existing installs)** →
  `<home>/records`. `config.joshbrain_dir` is kept as a thin alias so older callers
  keep working.
- **Creation (Plan 2):** a new module `mcpbrain/records.py` provides
  `ensure_records_repo(repo_dir, *, git_name, git_email)` — it `git init`s the dir,
  sets a local git identity only if none exists (from `owner_full_name`/`owner_email`,
  else `mcpbrain`/`mcpbrain@localhost`), and stamps any missing scaffold files. The
  **daemon's drain path calls it before every write** (`drain._records_repo`), so a
  freshly-onboarded user gets a working repo on the first cycle with **no manual
  seed and no wizard step**. Idempotent; never clobbers existing files or identity.
- **Scaffold (Plan 2):** the minimal templates the writers require live as
  constants in `mcpbrain/records.py`: `state/decisions.md` (with the
  `"Append new decisions at the top. One line per decision."` anchor),
  `state/hot.md` (`## Just decided` anchor), `MEMORY.md`, `context/voice.md`, and a
  `memory/` dir. (The richer cowork-prompt scaffolding from `seed_joshbrain.py` is
  not required for the write path and stays in the dev seed tool.)
- **Naming:** the user-facing surfaces lose "joshbrain": the directory, the config
  key, `draft.py`'s voice path, and the MCP tool-description text all become
  `records`. The Python module file **`joshbrain_write.py` keeps its name** (it is an
  internal import, not a user-facing surface; renaming it would churn imports for no
  user benefit). The **service/agent labels** (`church.centrepoint.joshbrain.*`) and
  the `agent_errs.py` glob are renamed in **1.6 / Plan 3**, since they are coupled to
  the launchd/Task-Scheduler label work, not the records data layer.

### 1.6 Windows cadence parity + platform hardening

`install_agent/uninstall_agent/restart_agent` already branch
darwin/linux/win32, but the four records cadences (prune, context-health,
gardener, meeting-packs) are launchd-only and hardcode `/bin/bash`, `/bin/sh`,
and `.sh` wrappers.

- Add **Task Scheduler** generators (Windows) and **systemd timer** generators
  (Linux) for the four cadences, mirroring the launchd plists.
- Replace shell-wrapped commands with cross-platform invocations: prefer
  `python -m mcpbrain <subcommand>` (or the `mcpbrain` console script) over
  `/bin/sh -c "... && git ..."`. The "run then commit" logic moves into the
  Python entrypoint so it is identical on every OS.
- Harden `config.py:18`: the `os.uname()` call is currently safe only because
  `os.name == "nt"` is checked first; guard it explicitly (use `sys.platform` or
  `platform.system()`) so a future refactor can't expose the Windows
  `AttributeError`.

Service/label naming becomes org-neutral **and** drops "joshbrain" (the label
rename deferred here from 1.5): `church.centrepoint.joshbrain.*` →
`com.mcpbrain.records.*` for the four cadence labels in `agents.py`
(`_PRUNE_LABEL`, `_HEALTH_LABEL`, `_MEETING_PACKS_LABEL`, `_GARDENER_LABEL`), and
the daemon/tray labels `church.centrepoint.mcpbrain*` → `com.mcpbrain.*`. The
`agent_errs.py` `GLOB` (`church.centrepoint.joshbrain.*.err`) and its label-parse
move in lockstep. The cadence plists that reference the repo path use
`config.records_dir` (the `joshbrain_dir=`/`Path(home).parent/"joshbrain"`
arguments at `agents.py:474,522,537-538,553,558,566,576` are repointed). Each user
is on their own device, so there is no same-machine collision concern; the rename
is about not baking one org (or one person) into the service identity.

---

## Part 2 — Distribution & release model

### 2.1 Channel: GitHub Pages PEP 503 wheel index

Publish each release as a **wheel** to a **public GitHub Pages PEP 503 "simple"
index** hosted from a *separate* public dist repo (e.g. `mcpbrain-dist`). The
**source repo stays private**. This is a standard, documented pattern (a simple
index is just static files; pip and uv consume it via `--index` /
`[[tool.uv.index]]`).

- Users need **no account** (Pages is public-by-URL).
- The wheel is fetchable by anyone who learns the URL — acceptable because the
  bundled desktop OAuth client is non-confidential by Google's PKCE design and
  Python decompiles anyway, so gating the wheel buys negligible real security.
- **Critical config detail:** our index is marked `explicit = true` so uv pulls
  **only `mcpbrain`** from it and resolves all other dependencies (sqlite-vec,
  fastembed, pyobjc, igraph, …) from **PyPI**. Without this, uv would hunt for
  those deps on our index and fail.
- **PyPI-ready:** the same wheels publish to public PyPI later with one added
  `twine upload` step and nothing else changing.

Why not the alternatives (recorded for posterity): private GitHub tags require
each user to have repo access (an account); public PyPI exposes the package and
is discoverable; Google Workspace Drive gives real access control but needs
bespoke fetch code and a non-standard mechanism for negligible security gain.

### 2.2 Versioning & release artifact

- Move off the static `0.1.0` to **real semver**, bumped per release.
- A release = build the wheel → add it (and a changelog entry) to the Pages
  index → regenerate the index `index.html` files. This should be a scripted
  step (`bin/release.py` or a GitHub Action in the *private* repo that pushes the
  built wheel to the *public* dist repo).
- The maintainer controls rollout timing by *when they publish* — this is the
  staging/rollback control (publish a higher version to roll forward; a bad
  release is contained because it only reaches users when published).

### 2.3 Install: one hosted line, no clone

A single bootstrap script hosted on the Pages site does everything end to end:

- macOS/Linux: `curl -fsSL https://<org>.github.io/mcpbrain/install.sh | sh`
- Windows: `irm https://<org>.github.io/mcpbrain/install.ps1 | iex`

The script: installs `uv` if missing → `uv tool install --index
mcpbrain=<pages-url> mcpbrain` (index `explicit`) → registers the login agent →
opens the browser wizard. **No git clone, no persistent source checkout, no
build-from-source for the heavy deps** (they arrive as prebuilt PyPI wheels).

The existing `install/setup.{command,sh,ps1}` become thin wrappers (or are
replaced by the hosted scripts). `mcpbrain setup --repo-dir` and the persisted
`repo_dir` config become unnecessary for updates (see 2.4) and are removed or
ignored. A piped `curl | sh` is also the Gatekeeper-safe path: nothing is
written to disk as a quarantined downloaded app, so macOS does not block it
(a double-click `.command` *would* be blocked unsigned).

### 2.4 Update: silent auto-update

Replace the git-pull-on-clone `update.py` with an index-based update:

- The **daemon** owns the auto-update tick — it is the always-on background
  process, so it (not the tray) is the right home for a background check. It
  periodically checks the Pages index for a newer version, installs it in the
  background via `uv tool install --index … mcpbrain --upgrade` (or `uv tool
  upgrade mcpbrain` against the recorded index), then restarts the daemon and
  tray.
- **Silent** by default (user chose this): no prompt, no terminal. The status
  home shows "Up to date · vX" / "Updating…". A bad release is contained by the
  maintainer's publish control.
- `mcpbrain update` remains as the underlying command the auto-updater and the
  (hidden) CLI call, so the logic has one home.
- Update orchestration must respect the single-writer store lock: the updater
  stops the daemon cleanly (releasing the lock) before reinstall, then restarts.
  Restart of the tray follows the daemon.

### 2.5 Trust posture (documented honestly)

The README's trust section is updated: unsigned but package-managed;
Gatekeeper/SmartScreen avoided via the package-manager channel; the deferred
paid upgrades (signing/notarization, OAuth verification, public PyPI) are listed
as the path to a fully public product. The shared OAuth client provisioning and
~100 test-user cap notes stay.

---

## Part 3 — UX & experience (non-technical)

### 3.1 State-aware home + separate content dashboard

The browser root `/` becomes **one adaptive home**:

- **Incomplete state** → the onboarding wizard (linear, progress-tracked steps;
  each step a real task; value-first). Hand-holds through the two unavoidable
  rough spots: the Google "unverified app → Advanced → Continue" screen, and the
  "quit & reopen Claude Desktop" step.
- **Configured state** → the **status / control center** (3.3).

Onboarding is the *empty state* of the home, not a separate one-shot page — so a
user returning "to setup later" lands on live status, with settings reachable
from there.

`/dashboard` stays as the **content dashboard** — the brain's daily value
(today's brief, actions, meeting packs). Separate surface, separate job ("what's
in my brain" vs "is the app healthy").

### 3.2 Verified live status, never static instructions (core requirement)

Every step and connection reports a **real tri-state — not started / connected /
needs attention — backed by an actual probe**, with a "last verified" time. No
step ever shows an instruction that fails to update once done. The wizard step
and the status-home card are the *same component in different states*; when the
probe flips, the screen flips.

A small **connection-probe layer** backs `/api/status`: each connection returns
`{state, detail, last_verified}`. Probes:

| Connection | Probe |
|---|---|
| **Claude Desktop** | Two levels. **Registered** = mcpbrain entry exists in Claude Desktop's config (we wrote it). **Verified connected** = the MCP server has phoned home. When Claude Desktop launches mcpbrain's MCP server, `mcp_server.py` writes a heartbeat (timestamp) to app-dir and/or the daemon control API on startup. Home reads it: heartbeat seen → "✓ Connected to Claude (last seen …)"; never → "Not connected yet — quit & reopen Claude Desktop". This is the only reliable signal — Claude Desktop exposes no status API, so the server self-reports. |
| **Google** | Token present + validity probe (expiry / lightweight API call) → "Signed in as you@… ✓" / "Access expired — Reconnect". |
| **ClickUp** | API key present + test call resolving user + list id → "Connected · <list>" / "Key invalid". |
| **Backup** | On/off + last snapshot timestamp & result. |
| **Daemon** | control_port present + responsive (exists today). |
| **First sync** | counts > 0 + last-sync time → "Synced ✓" / "Waiting for first sync". |
| **Identity/orgs** (the 1.1 gate) | config present → "✓" / "Finish setup". |

The menu bar and home both render off this one source of truth, which also
powers the self-healing banners (3.5).

### 3.3 What the status home shows live

Ordered by the implicit questions a returning non-technical user asks:

1. **"Is it healthy?"** — one big status (Running / Paused / **Needs
   attention**); last sync; current version + "up to date".
2. **"Is my data flowing?"** — counts (emails / docs / calendar synced, items
   indexed, people & projects in the graph); "syncing now…" activity; last-sync
   time.
3. **"Are my connections good?"** — the connection cards from 3.2, each with
   state + a one-click fix (e.g. **Reconnect Google**) when broken.
4. **"What does it want from me?"** — review/capture queue ("N to review"); any
   plain-language error with a fix button; the 1.1 gate as a "Finish setup"
   card.
5. **"Can I change something?"** — settings, secondary: cadences (proactive,
   backup on/off), pause, edit identity/orgs/ClickUp.
6. **"Where's my data going?"** — a short privacy reassurance ("Everything stays
   on this Mac; backup is off").

### 3.4 Menu bar (glance-first)

The menu-bar tray is a **first-class, non-optional component** of the app, not a
best-effort add-on. On every desktop install (macOS/Windows — the target
audience) it is installed as a managed login agent alongside the daemon, and a
failure to install it is a **surfaced error**, not a silent skip (today
`setup.py:_install_tray_best_effort` swallows it and the README calls it
"optional" — both change). For a non-technical user the tray *is* the app's face:
it is how they see status, get the attention/self-healing signals, and trust that
syncing is happening. The only case it is absent is a genuinely headless server
with no GUI, which is not the user-facing audience.

Following Dropbox/Backblaze conventions (status + one timestamp + the 1–2 most
common actions; icon encodes state; settings live in the window, not the menu):

- **Icon states:** Running / Syncing / Paused / **Needs attention**.
- **Title/tooltip:** `mcpbrain — Running · 12,400 items · synced 2m ago` or
  `Needs attention: reconnect Google`.
- **Menu:** status line, last-sync, Pause/Resume, "N to review →", a *contextual*
  "Reconnect Google" (only when broken), "Open mcpbrain" (the home),
  "Up to date · vX", Quit.

The tray today already has Pause/Resume, item count, Open setup, Open dashboard,
Quit, and a poll loop — this extends `status_text()` / `menu_items()` and the
icon to encode the new states.

### 3.5 Self-reporting + self-healing (cross-cutting)

Every failure mode (token expired/revoked, daemon down, identity unset, disk
full, ClickUp key invalid) becomes a **plain-language banner with a one-click
fix**, shown in **both** the menu bar (attention icon) and the home. Default: an
**OS notification only for critical, actionable problems** (opt-out in
settings), since non-technical users will not go looking. Notifications are not
chatty — they fire for "you must act" states, not routine activity.

---

## Part 4 — Backfill (indexing + enrichment)

So a new install becomes useful immediately and can be caught up to full history,
without manual scripts. Most of the indexing side already exists; the enrichment
side exists but is wired for Nexus and must be made on-device.

### 4.1 Indexing backfill — newest-first, full history (already built; make it first-class)

The machinery is in `mcpbrain/sync/__init__.py`. `run_sync_cycle()` runs live
delta-sync **first**, then one `progressive_backfill_step()`: it walks **one
90-day window per source, newest → oldest** (200 items/source/cycle), tracking a
per-source "floor" cursor (`<source>_backfill_until`) and an empty-window counter
(`<source>_backfill_empty`). There is **no fixed horizon** — a source stops only
after ~1 year of consecutive empty windows, so a 20-year-old account backfills in
full and then goes idle. It covers Gmail, Drive, and Calendar and never touches
the live delta cursors. Recent data lands first, so search is useful immediately
while history fills in behind it.

Product changes (small): backfill is **on by default** for new installs; it runs
**pre-config** (indexing/sync is identity-agnostic — it produces no mis-attributed
graph records, so a partially-onboarded user still sees search populate during
onboarding); and its progress is **surfaced** (4.3). The fail-loud gate (1.1)
does **not** apply to indexing backfill.

### 4.2 Enrichment backfill — one-shot, via local Claude Code

The enrichment contract already exists: `prepare` writes `enrich_queue/pending.json`
from unenriched chunks → an **extractor** (a Claude session) writes
`enrich_inbox/<batch>.json` → `drain` validates and applies it. The script you
remembered is `extractor_driver.run_extractor()` — but its default `run_claude`
lazily imports `claude_pool`, which is **Nexus-only**
(`PYTHONPATH=/home/josh/ops-brain/src`). It must be made to run on any user's
device.

- **Local Claude Code runner (new):** a `run_claude` implementation that shells
  to the on-device `claude` CLI, reusing `draft.py`'s binary resolution
  (`CLAUDE_BIN` → `PATH` → `~/.local/bin/claude`) — the same approach the gardener
  already uses. This lets any install enrich its backlog with the user's installed
  Claude Code: no Gemini key, no Nexus.
- **One-shot, user-triggered action** ("Backfill history enrichment"): drains the
  pending spool **newest-first**, batch by batch, via the local runner until the
  spool is empty (or a user-set bound), each batch validated + applied through the
  existing `drain`. It is **not** a new ongoing `enrich_mode` — **ongoing
  enrichment stays on the existing spool/cowork path** (the user's cowork already
  enriches ~100/hour). The one-shot action exists to catch up the *backlog* faster
  than the ongoing rate, and to populate a fresh install.
- **Gating:** enrichment writes owner identity + org taxonomy into the graph, so
  the **fail-loud gate (1.1) applies** — the action is unavailable until identity
  + ≥1 org are configured. (Contrast 4.1, which is ungated.)
- **Ordering:** newest-first — `prepare`'s unenriched-chunk selection orders by
  recency so the recent graph lands first, matching the indexing backfill.
- **Cost honesty:** it spends the user's local Claude Code usage. The UI states
  this plainly and shows progress (pending remaining / batches drained) with a
  cancel control. The spool is naturally resumable, so a cancelled or interrupted
  run resumes where it left off.

### 4.3 UI surfacing (extends Part 3)

- **Status home** shows indexing-backfill progress per source ("Indexing history —
  reached <date>") and a per-source "history complete" state, read from the floor
  cursors via `status()`.
- An **"Enrich history with Claude Code"** card shows the pending count, a
  Start/Cancel control, live progress, and the cost note. The card is gated behind
  the 1.1 config state (shows "Finish setup first" until identity + org are set).

## Data model / config changes

- `config.json` new/normalized keys: `owner_*` (neutral defaults),
  `orgs` (taxonomy: names + domains), `clickup_user_id`, `clickup_list_id`,
  `clickup_org_field_id`, `records_dir`, `auto_update` (default on),
  `notifications_critical` (default on). Existing `repo_dir` retired.
- New app-dir artifacts: `records/` (the local git repo), an MCP heartbeat marker
  (file in app-dir or a control-API-tracked timestamp), and the recorded install
  index URL (so the updater knows where to upgrade from).
- `version` becomes dynamic semver (single source, read by the updater and shown
  in the UI).

## New / changed code (by area)

- `config.py` — neutralize owner defaults; empty org default; add `clickup_*`
  helpers, `records_dir`, `auto_update`, `notifications_critical`; harden the OS
  branch.
- `orgs.py` — empty `DEFAULT_TAXONOMY`; `taxonomy_from_config` sole source.
- `daemon.py` — the 1.1 gate (skip enrichment until configured); `status()`
  returns `configured` + the connection-probe results; auto-update tick;
  self-healing state.
- `clickup.py`, `clickup_sync.py`, `mcp_server.py` — remove Josh literals; route
  through config (Plan 1). `clickup.py` `_normalise_task`/`create_task` read the
  per-user ClickUp helpers.
- `draft.py` — `generate_draft` takes `owner_full_name` (Plan 1); `_load_voice_rules`
  reads `config.records_dir(home)/context/voice.md` (Plan 2).
- `config.py` — `records_dir()` + `joshbrain_dir()` back-compat alias (Plan 2).
- `mcpbrain/records.py` (**new**, Plan 2) — `ensure_records_repo()` (git init +
  scaffold templates), idempotent, no-clobber.
- `drain.py` — `_records_repo(home)` resolves `records_dir` and ensures the repo
  before the writer block (Plan 2). `joshbrain_write.py` keeps its filename; only
  its `owner` default and docstring change.
- `agents.py` — org-/person-neutral labels (`com.mcpbrain.records.*`); repo paths
  via `config.records_dir`; Task Scheduler + systemd timer generators for the four
  cadences; replace shell wrappers with `python -m mcpbrain …`. `agent_errs.py` glob
  + label-parse follow the rename. (Plan 3.)
- `update.py` — index-based reinstall replacing git pull; lock-safe restart.
- `setup.py` / `install/*` — thin one-line bootstrap; drop `--repo-dir`
  persistence; **tray installed as a required desktop component** (replace
  `_install_tray_best_effort`'s silent swallow with a surfaced error on desktop;
  only a headless/no-GUI box may skip it).
- `agents.py` — tray login agent registered as a managed component on every
  desktop install (same lifecycle as the daemon agent), not best-effort.
- `control_api.py` — extend `/api/status` with probes; add MCP heartbeat
  endpoint; reconnect/update/notification-related routes; enrichment-backfill
  action (start/cancel/progress).
- `mcpbrain/sync/__init__.py` — expose the backfill floor/empty cursors in
  `status()` for the indexing-backfill progress UI (4.1); the walk itself is
  unchanged.
- `extractor_driver.py` — add a **local Claude Code `run_claude`** (reuse
  `draft.py`'s `CLAUDE_BIN → PATH → ~/.local/bin/claude` resolver) and a
  drain-until-empty one-shot entry for the enrichment backfill (4.2).
- `prepare.py` — order unenriched-chunk selection newest-first (4.2).
- `mcp_server.py` — write heartbeat on startup (and periodically).
- `wizard/index.html` — collect identity/orgs/ClickUp; render the wizard as the
  empty state of the state-aware home; verified-status step components.
- new status-home assets (or extend `index.html`) — the control center (3.3).
- `tray.py` — icon states, richer status line, contextual reconnect, version.
- `bin/release.py` (new) or CI — build wheel + publish to the Pages index.
- `bin/seed_joshbrain.py`, `bin/seed_from_nexus.py` — demoted to dev-only /
  generalized; not part of the user onboarding path.

## Testing

- **Gate (1.1):** daemon skips enrichment when unconfigured; runs once
  identity + ≥1 org present; `status().configured` reflects both.
- **Neutralized defaults (1.2):** an empty config yields no Josh values; no path
  attributes to Josh.
- **Bypasses (1.3/1.4):** draft/MCP/joshbrain/clickup paths use configured owner;
  ClickUp uses configured ids.
- **Records repo (1.5):** `git init` in app-dir; commits work; rename complete
  (no "joshbrain" literals in code paths); scaffolding stamped idempotently.
- **Cadence parity (1.6):** generators emit valid Task Scheduler XML / systemd
  units; commands are shell-free; `os.uname` guard holds on a simulated nt.
- **Index/update (2):** install resolves mcpbrain from the index and deps from
  PyPI (`explicit`); update upgrades to a higher published version and is
  lock-safe; a same-version no-op does nothing.
- **Probes (3.2):** each probe returns correct tri-state for present/absent/
  broken inputs; the Claude heartbeat flips "registered" → "connected" when the
  MCP server writes its marker; staleness handled.
- **UX states:** home renders wizard when incomplete and control center when
  configured; menu bar icon/title reflect each state; self-healing banner +
  critical notification fire on a simulated token-expired.
- **Backfill (Part 4):** indexing backfill runs pre-config and newest-first, and
  its floor cursors surface in `status()`; the local Claude Code runner shells to
  a fake `claude` binary (no network); the one-shot enrichment backfill drains the
  spool newest-first until empty with each batch validated + applied; the gate
  blocks the enrichment backfill until identity + ≥1 org are configured; a
  cancelled run resumes from the remaining spool.

## Risks / notes

- **Silent auto-update + a bad release** reaches everyone on next check. Mitigated
  by semver + maintainer publish control; consider a simple "skip version N" /
  rollback by publishing N+1. No staged cohort at $0.
- **GitHub Pages is public.** The dist repo and wheels are public-by-URL; source
  stays private. Accepted (PKCE client non-secret; Python decompiles).
- **Workspace tie-in deferred:** distribution is Workspace-independent; if outside
  users are added later, the Pages index already serves them (no change).
- **OAuth ~100 test-user cap and the "unverified app" warning** remain until the
  paid verification path is taken. The wizard makes the warning feel expected.
- **Claude Desktop connection** can only ever be *self-reported* by the MCP
  server; if a user never opens Claude Desktop, "connected" legitimately stays
  "not seen yet" — copy must make that distinction clear (registered vs verified).
- **Enrichment backfill cost/duration:** a large backlog drained through the local
  Claude Code CLI can be long and spends real Claude usage. Mitigated by making it
  one-shot/opt-in (not background), newest-first (early value), cancellable, and
  resumable (the spool persists). Ongoing enrichment stays on the existing
  spool/cowork path, so the backfill is a catch-up, not the steady state.

## Rollout sequencing

1. **Part 1** first (correctness) — it removes the silent-Josh failure mode and
   is a precondition for trusting a multi-user install. The 1.1 gate is the
   keystone.
2. **Part 3 status layer** (3.2 probes + `status()` shape) next — it is what
   makes Part 1's gate and Part 2's update *visible*, and the UX renders off it.
3. **Part 2** (index + one-line install + silent update) — the delivery, once the
   app it delivers is correct and self-reporting.
4. **Part 3 polish** (state-aware home, menu bar states, self-healing
   notifications) — layered on the status layer.
5. **Part 4 backfill:** indexing backfill (4.1) is independent and can land early
   (it mostly exists — surface progress + confirm on-by-default). Enrichment
   backfill (4.2) follows Part 1 (it depends on the gate) and the status layer (it
   surfaces progress there).

Each part is a separable implementation plan; this spec is the shared design.

### Plan mapping (this spec → plan files)

The work is decomposed into a plan series under `docs/superpowers/plans/`. Plan
*numbers below reflect authoring order, not a mandated execution order* — the
recommended dependency order is the "Rollout sequencing" list above (correctness →
status layer → distribution → UX → backfill). Part 1 splits because the records
data layer and the platform/label work are independent:

- **Plan 1** — `2026-06-09-part1-identity-correctness.md` — spec **1.1–1.4** (gate
  predicate + daemon gating, neutralized owner/orgs defaults, ClickUp per-user
  helpers + routing, the four literal-"Josh" bypasses). *Written.*
- **Plan 2** — `2026-06-09-part2-records-repo.md` — spec **1.5** records data layer
  (`config.records_dir`, `mcpbrain/records.py` ensure+scaffold, drain ensures the
  repo, voice path, user-facing de-"joshbrain"). *Written.*
- **Plan 3** — platform & cadences — spec **1.6** (`agents.py` label rename
  `com.mcpbrain.records.*`, `agent_errs.py` glob, Task Scheduler + systemd timer
  generators, `os.uname` hardening). *To write.*
- **Plan 4** — status & connection-probe layer — spec **3.2** + `daemon.status()`
  `configured`/probe fields (also unblocks the 1.1 UI surfacing). *To write.*
- **Plan 5** — distribution & release — spec **Part 2**. *To write.*
- **Plan 6** — UX surfaces — spec **3.1/3.3/3.4/3.5**. *To write.*
- **Plan 7** — backfill — spec **Part 4**. *To write.*
