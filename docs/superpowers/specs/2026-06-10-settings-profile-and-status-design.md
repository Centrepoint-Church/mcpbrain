# Guided onboarding: settings/profile, MCP-registration status, Cowork projects & skill automation

**Date:** 2026-06-10
**Status:** implemented, then partly superseded.
> **Superseded by `2026-06-11-cowork-skills-and-mcp-context-design.md`** for: the
> `cowork_tasks.py` / scheduled-dir enrichment write (now a personal skill in
> `~/.claude/skills/`), the "two Cowork projects" step (now one "My Brain" project
> created from scratch + a `/mcpbrain-setup` skill), the records-`CLAUDE.md`
> classification rules (moved to the enrichment skill), and context delivery (now
> via MCP resources). The rest of this spec (prefill, timezone dropdown,
> registration status, memory hooks, backfill) stands as built.

## Problem

Six gaps in the post-install experience, all in the daemon control API + wizard:

1. **No pre-fill.** The setup form (`mcpbrain/wizard/index.html`) can only *write*
   config (POST `/api/config`); it never reads saved values back. Every revisit
   shows empty fields. The user wants it to behave like a settings/profile page —
   name, email, role, orgs, timezone, ClickUp all pre-filled with what was saved.

2. **Timezone is free text.** `<input id="timezone">` is an error-prone text box.
   The user wants a dropdown listing locations with their GMT offset, e.g.
   `Australia/Perth (GMT+08:00)`.

3. **Claude registration status is binary and misleading.** `probe_claude` only
   knows "heartbeat seen / not seen". It can't tell *not registered* from
   *registered but Claude Desktop not yet restarted*. We have direct read access
   to `claude_desktop_config.json`, so registration is directly checkable.

4. **Cowork enrichment setup is a giant copy-paste.** The wizard makes the user
   paste a multi-page extraction spec into Claude Desktop's Cowork to stand up the
   `mcpbrain-enrichment` scheduled task. There is no status for whether it exists.

5. **No hand-holding for a non-technical user.** The wizard has bare fields and no
   guidance: someone with zero context doesn't know how to get a ClickUp token,
   find a List ID, set up the two Cowork projects (system vs daily-work), attach
   the schedule to a project, or turn on cross-session memory. Setup must walk
   through *every* element with real, anchored instructions + screenshots, assume
   nothing, and pre-create/configure everything it safely can.

6. **No cross-session memory by default.** Claude Code-style "remember across
   sessions" relies on SessionStart/SessionEnd hooks (the joshbrain determinism
   layer). A fresh install has none, so Cowork forgets between sessions. The product
   should install these hooks (cross-platform) as a guided, one-click step.

## Research findings (Cowork constraints)

From the official docs (code.claude.com/docs/en/desktop-scheduled-tasks) and the
on-disk layout of this machine:

- A scheduled task's **prompt** is a plain file: `<scheduled-dir>/<task>/SKILL.md`,
  with YAML front-matter (`name`, `description`) + body. **This is author-able.**
- The task's **schedule (cadence), working folder, model, and enabled state are
  NOT in any file** — they are app/server state, changed only via the Routines/Edit
  UI, by asking Claude in a Desktop session, or the `update_scheduled_task` MCP tool.
- Those MCP tools belong to **Claude Desktop**, not to an external process. The
  mcpbrain daemon therefore **cannot register a schedule's cadence/enabled state**.
- On this machine the Cowork path is `~/Documents/Claude/Scheduled/` (macOS).
  The Claude-Code-Desktop docs cite `~/.claude/scheduled-tasks/`. Both are
  undocumented-stable and may differ by product/version, so path resolution must
  be defensive.

**Decision (locked with user):** mcpbrain will **auto-write/refresh the SKILL.md
and show enrichment status**, but will **not** attempt to set the cadence. The
existing instructions for enabling the schedule in Claude Desktop stay. Cowork
path resolution uses fallbacks and **degrades gracefully** (skip writing + show a
hint) rather than ever crashing.

## Locked decisions

| Decision | Choice |
|---|---|
| Settings access when configured | **Always show the pre-filled form inline** (configured home = status summary + editable settings form) |
| ClickUp token pre-fill | **Masked, blank-to-keep** — never echo the secret; blank field keeps existing |
| Timezone list | **Curated subset, ≥1 location per UTC offset** (−12 … +14), labelled `Location (GMT±HH:MM)` |
| Cowork automation scope | **Auto-write SKILL.md + status only** (no cadence registration, no new paste-line) |
| Cowork path fragility | **Resolve with fallbacks + degrade gracefully** |
| Configured-view ordering | **Status summary on top, settings form below** |
| Onboarding guidance | **Inline expandable help per step** — short always-visible explanation + a "Show me how" expander with screenshot, numbered steps, Copy buttons |
| The two Cowork projects | Both point at **existing mcpbrain-managed folders**, not new seeded dirs: **Enrichment project** → working folder = the mcpbrain home (`~/.mcpbrain`, where `enrich_queue/` + `enrich_inbox/` live), hosts the scheduled task. **"My Brain" working project** → working folder = the **records repo** (`records_dir = <home>/records`, read+write) + `~/.mcpbrain` connected read + mcpbrain MCP attached. |
| How "My Brain" gets its instructions | **Enrich the records-repo scaffold** (`records.py`) with a `CLAUDE.md` + `context/`/`reference/` templates (profile-interpolated), modelled on `~/joshbrain/CLAUDE.md` and `cowork/context-project.md`. The working project's instructions ARE the records-repo `CLAUDE.md`. |
| Project registration | The user registers each Cowork space in-app (we do NOT write `spaces.json`). We don't create folders under `~/Documents/Claude/Projects/`; both projects target folders mcpbrain already owns. |

## Cowork facts (anchored, researched 2026-06-10)

- **Two distinct "project" concepts**: *Claude Projects* (cloud, claude.ai, context/
  memory) vs *Cowork Projects* (local, folder-based, execution). We target **Cowork
  Projects** — local folders under `~/Documents/Claude/Projects/<Name>/`, registered
  in `local-agent-mode-sessions/<account>/<workspace>/spaces.json`
  (`{id, name, folders:[{path}], origin, createdAt, updatedAt}`). That registry is
  app-managed per-account/workspace → we do not write it.
- **Create a Cowork project**: Projects → **+** → **Use an existing folder** → pick
  the folder → name + Create. **Scheduled tasks can be specific to a project.**
- **ClickUp API token**: avatar (top-right) → **Settings → Apps → API Token →
  Generate → Copy** (token begins `pk_`).
- **ClickUp List ID**: right-click the List in the sidebar → **Copy link** → the
  number after `/li/` in the URL (`…/v/li/<list_id>`).

### Memory-architecture alignment (joshbrain blueprint)

The working project mirrors `~/joshbrain/docs/superpowers/specs/2026-06-09-cowork-memory-architecture-design.md`
and `~/joshbrain/cowork/context-project.md`. Key inherited facts:

- The working Cowork project's folder is the **records repo** (the product's
  rename of `joshbrain`): `records_dir = config.records_dir(home) = <home>/records`,
  created/scaffolded by `records.ensure_records_repo`. The user edits
  `context/`/`reference/` there; the daemon owns `state/decisions.md`,
  `state/hot.md`, `memory/` via the existing **write tools** (`brain_decision`,
  `brain_note`, `brain_memory_write`) — already shipped. Ownership split avoids
  git races; **writes are queued** (one daemon cycle), never hand-edited.
- `~/.mcpbrain` (the home) is the runtime (index, daemon, `enrich_queue/`,
  `enrich_inbox/`) and is the **enrichment project's** working folder.
- Determinism order from that design — **hooks > MCP tools > instructions** — is
  why writes route through tools, not instructed file edits, and why the
  SessionStart priming + SessionEnd capture hooks are **in scope** here (shipped as
  cross-platform `mcpbrain` subcommands, installed into `~/.claude/settings.json`).
- Today `records.ensure_records_repo` stamps only the writer anchors
  (`state/decisions.md`, `state/hot.md`, `MEMORY.md`, `context/voice.md`,
  `memory/`). It does **not** stamp a `CLAUDE.md` or identity/preferences/reference
  — that is the gap this spec fills.

## Architecture

All work is in existing layers (`probes.py`, `daemon.py`, `control_api.py`,
`records.py`, `cli.py`, `wizard/index.html`) plus three small new modules
(`timezones.py`, `cowork_tasks.py`, `hooks.py`) and package data
(`cowork/enrichment.md`, `records_templates/`, `wizard/img/`). No new third-party
dependencies (`zoneinfo` is stdlib).

### New / changed backend

**`mcpbrain/timezones.py` (new, isolated, unit-tested)**
- `CURATED_ZONES: tuple[str, ...]` — IANA names, at least one representative per
  UTC offset from −12 to +14 (e.g. `Pacific/Midway`, `Pacific/Honolulu`,
  `America/Los_Angeles`, …, `Australia/Perth`, `Asia/Tokyo`, `Pacific/Auckland`,
  `Pacific/Kiritimati`).
- `offset_label(zone, *, now) -> str` — returns `"<zone> (GMT±HH:MM)"`, computing
  the offset via `zoneinfo.ZoneInfo` at `now` (DST-correct). `now` is injected so
  tests are deterministic.
- `zone_options(*, now) -> list[dict]` — `[{"value","label"}]` for the curated set,
  sorted by offset then name.
- Contract: every UTC offset in −12…+14 has ≥1 entry; all labels match
  `^.+ \(GMT[+-]\d\d:\d\d\)$`; all values are valid IANA zones.

**`mcpbrain/cowork_tasks.py` (new, isolated, unit-tested)**
- `scheduled_dir() -> Path | None` — resolve the Cowork scheduled-tasks dir,
  biased to Cowork (the product target). Order: (1) `~/Documents/Claude/Scheduled`
  if it or its `~/Documents/Claude` parent exists; (2) `~/.claude/scheduled-tasks`
  (honouring `CLAUDE_CONFIG_DIR`) only if that dir *itself* already exists — so the
  near-ubiquitous `~/.claude` (Claude Code CLI) can't hijack a Cowork install; (3)
  default to the Cowork dir (to be created) when `~/Documents` exists; else `None`.
  Pure path logic; filesystem root injectable for tests.
- `ENRICHMENT_TASK = "mcpbrain-enrichment"`.
- `write_enrichment_skill(home) -> Path | None` — read the canonical skill body
  from package data `mcpbrain/cowork/enrichment.md`, render front-matter + body,
  write `<scheduled_dir>/mcpbrain-enrichment/SKILL.md` atomically. Returns the
  path, or `None` if `scheduled_dir()` is `None` (degrade, never raise). Idempotent.
- `enrichment_skill_present() -> bool` — does that SKILL.md exist.

**`mcpbrain/cowork/enrichment.md` (new package data)** — the canonical enrichment
extraction prompt, **extracted verbatim from the current inline `spec-task` block
in `index.html`** so there is one source of truth. The wizard renders it; the
daemon writes it to the Cowork SKILL.md. Front-matter (`name: mcpbrain-enrichment`,
`description: …`) is added by `write_enrichment_skill`.

**`mcpbrain/probes.py`**
- `probe_claude(home)` gains registration awareness:
  - no mcpbrain entry in `claude_desktop_config.json` → `not_started`,
    `"Not registered yet — finish setup"`.
  - entry present, no/stale heartbeat → `needs_action`,
    `"Registered — quit & reopen Claude Desktop"`.
  - entry present, fresh heartbeat → `ok`, `"Connected"`.
  - reads `claude_desktop_config.json` via the existing
    `wizard.register.claude_desktop_config_path()`; a missing/malformed file
    degrades to the heartbeat-only behaviour (never raises).
- `probe_enrichment(home)` (new) → `not_started` (no SKILL.md), `ok`
  (SKILL.md present **and** an `enrich_inbox/*.json` produced **within the last 2
  days** → `"Running"`), or `needs_action` (SKILL.md present, no output in 2 days →
  `"Set up the schedule in Claude Desktop"`).
- `all_connections` adds `"enrichment"` to the returned dict.

**`mcpbrain/daemon.py`**
- `Daemon.config_profile() -> dict` (new) — read `config.read_config(app_dir())`
  and project ONLY: `owner_full_name, owner_name, owner_email, owner_role, orgs,
  clickup_list_id, timezone`, plus `clickup_api_key_set: bool`
  (`bool(clickup_api_key)`). **Never returns the raw key.**
- `apply_config` (existing) calls `cowork_tasks.write_enrichment_skill(home)` **and**
  `records.scaffold_records(home)` after a successful write, so saving settings both
  (re)materialises the enrichment SKILL.md and stamps/refreshes the records-repo
  working-project scaffold (profile interpolated). Both are best-effort: a failure
  degrades silently (logged at debug) and never fails the POST.

**`mcpbrain/control_api.py`**
- `do_GET`: add `GET /api/config` → `h_json(200, daemon.config_profile())` and
  `GET /api/timezones` → `h_json(200, {"zones": timezones.zone_options(now=datetime.now(timezone.utc))})`
  (the control API passes the current UTC time; the module computes each zone's
  offset against it). Both are token-guarded like every other control route.
- These are **one-shot reads**; they are deliberately NOT folded into
  `/api/status` (which the page polls every 3 s) so a poll can never overwrite
  what the user is typing.

**`mcpbrain/records.py` (extend) + `mcpbrain/records_templates/` (new package data) —
enrich the records-repo scaffold so the working Cowork project has real instructions**
- New package-data templates under `mcpbrain/records_templates/`, modelled on
  `~/joshbrain` (genericised, no Josh content):
  - `CLAUDE.md` — a **full** project-instructions file mirroring `~/joshbrain/CLAUDE.md`
    section-for-section (genericised, not a lean starter): `@context/identity.md` /
    `@context/voice.md` / `@context/preferences.md` imports; a gardener-protected
    identity block; an **org-tagging** block built from the user's configured `orgs`;
    **role-attribution** rules; the **Memory Protocol** (read tools: `brain_search`,
    `brain_read`, `brain_context`, `brain_actions`, `brain_graph`, `brain_proactive`,
    `brain_draft_reply`/`brain_draft_refine` + the load-on-demand order); the
    **"Where Things Go"** write-routing table (`brain_decision` / `brain_note` /
    `brain_memory_write` / `brain_ingest`, all QUEUED, daemon-owned — do not
    hand-edit those files); output-file convention; quality standard; planning &
    retirement-check rules; proactive behaviours; session-capture rules;
    self-evolution protocol; platform notes (records repo = working tree,
    `~/.mcpbrain` = runtime; the three surfaces). The user's name/role/orgs are
    interpolated; org-specific examples become the configured org names.
  - `context/identity.md` — name/role/orgs interpolated from config; the rest left
    as guided placeholders for the user to fill.
  - `context/preferences.md`, `reference/systems.md`, `reference/projects.md` —
    placeholder templates with section headings + "fill this in" guidance.
  - (`context/voice.md`, `state/*`, `MEMORY.md`, `memory/` already scaffolded.)
- `ensure_records_repo` gains a `profile: dict | None` arg; on first creation it
  also stamps the new templates with `{owner_full_name, owner_role, orgs}`
  interpolated. **Never clobbers an existing file** (write-if-absent), so a user's
  edited `CLAUDE.md`/`identity.md` survives re-runs. `CLAUDE.md`'s org-tagging block
  regenerates only when absent.
- `scaffold_records(home) -> list[Path]` thin wrapper: resolve `records_dir`, read
  the profile from config, call `ensure_records_repo(..., profile=...)`, return the
  paths stamped. Called from `apply_config` (best-effort) so saving settings
  materialises/refreshes the working-project scaffold. Degrades silently on error.
- `records_status(home) -> dict` — `{present: bool, has_claude_md: bool,
  path: str}` for the status card; read-only, degrades to `present=False`.

**`mcpbrain/hooks.py` (new) + two CLI subcommands — the determinism layer**
Replaces Josh's bash hooks with cross-platform `mcpbrain` subcommands the hooks
invoke directly (mcpbrain is on PATH on macOS/Windows/Linux; shell scripts are not
portable):
- `mcpbrain session-start` — prints priming context to stdout: the recent
  `state/hot.md` continuity lines from `records_dir` + open actions (read
  `control_port`/`control_token`, GET `/api/dashboard/today`). Bounded (≤8 lines
  each), never hard-fails — a missing repo/daemon prints a short "(unavailable)"
  note. This is the Python port of `session_prime.sh`.
- `mcpbrain session-end` — reads the Claude Code hook JSON from **stdin**, parses
  the transcript at `transcript_path`, and writes a session-summary capture to the
  spool (reusing `capture.write_capture`, same envelope as `/api/session/ingest`).
  Skips trivial/headless single-shot runs so the brain isn't flooded. Python port
  of `session_extract.sh`; stdin is read as a stream, never shell-interpolated.
- `hooks.py`: `install_session_hooks() -> Path` merges `SessionStart` +
  `SessionEnd` `command` hooks (calling `mcpbrain session-start` / `session-end`)
  into `~/.claude/settings.json` (honouring `CLAUDE_CONFIG_DIR`). Idempotent + merge-
  safe: preserves any existing hooks/keys, refuses to clobber a malformed file
  (same atomic-write + 0600 pattern as `wizard.register`), and does not duplicate
  an already-present mcpbrain hook entry. `hooks_status() -> dict`
  (`{installed: bool}`) for the status card; `uninstall_session_hooks()` for symmetry.
- These hooks are **user-scope** (`~/.claude/settings.json`), so they fire in every
  Claude Code AND Cowork session (interactive + scheduled) — exactly the
  determinism the joshbrain architecture relies on.

**`mcpbrain/wizard/img/` (new package data) + static route** — onboarding
screenshots shipped with the package. `control_api.do_GET` serves
`GET /img/<name>` from this dir (whitelisted filenames, `image/png`, no token
required — generic product art, no secrets). Missing file → 404, never raises.

### Frontend (`mcpbrain/wizard/index.html`)

- **Pre-fill on load:** a one-shot `GET /api/config` after first paint populates
  name (from `owner_full_name`), email, role; rebuilds one org row per saved org
  (domains joined by `", "`); sets `clickup_list_id` and the timezone `<select>`.
  If `clickup_api_key_set`, the token field placeholder becomes
  `"•••• configured — leave blank to keep"` and stays empty.
- **Timezone dropdown:** replace `<input id="timezone">` with `<select id="timezone">`
  populated from `GET /api/timezones`; pre-select the saved zone, else the
  browser-detected `Intl…timeZone` if it is in the list. `saveProfile()` reads
  `.value` unchanged.
- **No save-path change:** `saveProfile()` already posts only non-blank fields
  (`write_config` merges), so masked-blank-token "keep existing" works as-is.
- **Status-first configured view:** move the self-contained `#home-status` section
  to render *above* the wizard `<main>`. `renderHome(j)`:
  - unconfigured → show `<main>` (full wizard), hide `#home-status`.
  - configured → show `#home-status` (status cards) **and** `#step-profile`
    (pre-filled settings form with Save); hide the other steps
    (`#step-google`, `#step-enrich`, `#step-register`, `#step-status`). Reword the
    `#step-profile` heading to "Your settings" in the configured state.
- **New connection cards:** `renderConnections` order array gains `"enrichment"`;
  the Claude card now reflects the three registration states from `probe_claude`.

#### Guided onboarding (the "walk them through every element" change)

**Nothing is assumed.** Every element needed for a fully working install is an
explicit, walked-through step — no "you probably already…" gaps. Each wizard step
gets a short, always-visible plain-language explanation of *what it is and why*,
plus a **"Show me how" expander** revealing a screenshot + numbered steps + Copy
buttons. Written for someone with zero prior context. A persistent checklist down
the side shows which steps are done (driven by the live connection states) so the
user always knows what's left. Steps:

1. **Connect Google** — what read-only access means; screenshot of the
   "Google hasn't verified this app → Advanced → Continue" consent screen so the
   scary warning is expected.
2. **About you** — why name/role/orgs matter (attribution + classification).
3. **ClickUp (optional)** — expander with the anchored token steps (avatar →
   Settings → Apps → API Token → Generate → Copy) and List-ID steps (right-click
   List → Copy link → number after `/li/`), each with a screenshot and an
   "Open ClickUp" link. Timezone dropdown sits here (required for deadlines).
4. **Connect to Claude Desktop** — the Register button (writes the MCP entry) +
   "fully quit & reopen Claude Desktop" with a screenshot; live registration status.
5. **Your two Cowork projects** — a new step. Explains *what each is for* and points
   each at a folder mcpbrain already owns (the exact path is shown with a Copy
   button, since Cowork's folder picker needs it):
   - **mcpbrain Enrichment** — "the engine room: a scheduled task that quietly turns
     your mail into structured memory every hour." Expander: create a Cowork project
     on the mcpbrain home folder (path shown, e.g. `~/.mcpbrain`), then add an
     **Hourly** scheduled task pointed at the auto-written `SKILL.md`. Screenshots
     for each click.
   - **My Brain** — "where you actually work with your brain — like Claude Code's
     memory, in Cowork." Expander: create a Cowork project on the **records repo**
     (path shown, e.g. `~/.mcpbrain/records`), connect `~/.mcpbrain` as a read
     folder, and attach the mcpbrain MCP. Its `CLAUDE.md` (just scaffolded) already
     tells Claude the identity, voice, memory protocol, and write-tool routing.
     Screenshot.
   - A **"Prepare my working space"** button calls `POST /api/records/scaffold`
     (→ `records.scaffold_records`), which creates + stamps the records repo
     (`CLAUDE.md`, context/, reference/) so it's ready before the user points Cowork
     at it. Status card shows records-repo-ready + enrichment-skill-installed.
6. **Memory hooks** — a new step. Explains in plain language that this makes Claude
   *remember across sessions automatically* (prime each session with recent context,
   capture each session at the end), the same mechanism Claude Code uses. A
   **"Turn on memory hooks"** button calls `POST /api/hooks/install`
   (→ `hooks.install_session_hooks`) and the status card flips to "On". Explains it
   edits `~/.claude/settings.json` (preserving anything already there) and applies to
   both Claude Code and Cowork.
7. **Status** — the configured home (already covered).

New control routes: `POST /api/records/scaffold` → `scaffold_records()`;
`POST /api/hooks/install` → `install_session_hooks()`; `records_status`,
`probe_enrichment`, and `hooks_status` are merged into `/api/status` (read-only, cheap).

## Data flow

```
page load ─► GET /api/timezones ─► populate <select>
        └─► GET /api/config (one-shot) ─► prefill fields (+ select saved tz)
        └─► every 3s: GET /api/status ─► renderStatus + renderHome (never touches form inputs)

Save ─► POST /api/config {non-blank fields} ─► write_config (merge)
                                            ├─► cowork_tasks.write_enrichment_skill() [best-effort]
                                            └─► records.scaffold_records(profile) [best-effort]

status poll ─► probes.all_connections + records_status + hooks_status
            ─► {google, claude(+registration), clickup, backup, records, enrichment, memory-hooks}

SessionStart hook ─► `mcpbrain session-start` ─► prints hot.md + open actions into context
SessionEnd   hook ─► `mcpbrain session-end` (stdin transcript) ─► capture spool ─► daemon drain
```

## Error handling

- Every new probe and the Cowork writer **degrade, never raise** — a missing
  config file, an unwritable Cowork dir, or a moved directory yields a graceful
  state or a no-op, consistent with the existing probe contract.
- `GET /api/config` never includes the ClickUp secret (bool only).
- `zoneinfo` lookups are wrapped; an unknown curated zone is skipped, not fatal.
- Hook install refuses a malformed `~/.claude/settings.json` (raises a clear error
  surfaced to the wizard) rather than overwriting it; the `session-start`/
  `session-end` subcommands never hard-fail a session (bounded output / silent skip
  on error) so a broken hook can't block Claude Code or Cowork from starting.

## Testing

- `tests/test_timezones.py` — offset coverage (−12…+14 all present), label regex,
  valid IANA values, deterministic with injected `now`.
- `tests/test_cowork_tasks.py` — `scheduled_dir` fallback order + `None` when no
  parent exists; `write_enrichment_skill` writes correct front-matter + body and
  is idempotent; returns `None` (no raise) when dir unresolved.
- `tests/test_probes.py` — `probe_claude` three states (not registered / registered
  no heartbeat / connected); `probe_enrichment` three states; malformed config
  degrades.
- `tests/test_control_api.py` — `GET /api/config` returns projected keys and
  `clickup_api_key_set` bool (asserts the raw key is absent); `GET /api/timezones`
  returns well-formed options; both require the bearer token.
- `tests/test_wizard_serve.py` — served HTML contains the `<select id="timezone">`
  and the prefill bootstrap; `#home-status` precedes `<main>`.
- `tests/test_daemon.py` — `config_profile` projection (no secret); `apply_config`
  invokes the skill writer best-effort (and a writer failure doesn't fail the POST).
- `tests/test_records.py` (extend) — `ensure_records_repo(profile=…)` stamps
  `CLAUDE.md` + `context/identity.md` with the profile interpolated; is idempotent;
  **never clobbers** a user-edited `CLAUDE.md`/`identity.md`; org-tagging block built
  from configured `orgs`; `scaffold_records` degrades silently when `records_dir`
  unresolved; `records_status` read-only.
- `tests/test_hooks.py` — `install_session_hooks` writes both hooks, is idempotent
  (no duplicate entry on re-run), preserves existing hooks/keys, refuses a malformed
  settings file, 0600; `hooks_status`; `uninstall_session_hooks` removes only ours.
- `tests/test_session_hooks.py` — `mcpbrain session-start` prints bounded
  continuity + actions and degrades when repo/daemon absent; `mcpbrain session-end`
  parses a sample transcript JSON on stdin into a capture envelope and skips a
  trivial/headless run.
- `tests/test_control_api.py` — `POST /api/records/scaffold` returns stamped paths;
  `POST /api/hooks/install` returns installed; `GET /img/<name>` serves a shipped PNG
  and 404s an unknown/sneaky name (path traversal rejected).

## Screenshot capture plan (maintainer action)

Screenshots are only needed for the **external UIs** a no-context user can't
navigate (ClickUp, Claude Desktop/Cowork, Google consent). Our own wizard UI is
shown live, so it needs none.

- **Format:** PNG, ~1200 px wide (retina source downscaled), light mode.
- **Redaction:** blur/replace any personal data (email, workspace name, real list
  names) — these ship publicly in the wheel.
- **Repo location:** `mcpbrain/wizard/img/` (package data; served at `/img/<name>`).
- **Manifest:** `docs/onboarding/SCREENSHOTS.md` lists each with its purpose.

| # | Filename | What it must show |
|---|----------|-------------------|
| 1 | `google-unverified-advanced.png` | Google consent "hasn't verified this app" → **Advanced** → Continue link |
| 2 | `clickup-settings.png` | ClickUp avatar menu open (top-right), **Settings** highlighted |
| 3 | `clickup-apps-token.png` | Settings → **Apps** → **API Token** section with Generate/Copy |
| 4 | `clickup-list-copylink.png` | Right-click a List in the sidebar → **Copy link** highlighted |
| 5 | `clickup-list-id-url.png` | The copied URL with the `…/li/<list_id>` portion highlighted |
| 6 | `claude-quit-reopen.png` | macOS menu bar **Claude → Quit** (caption: then reopen) |
| 7 | `cowork-projects-plus.png` | Cowork left nav **Projects → +** showing the 3 options |
| 8 | `cowork-use-existing-folder.png` | "Use an existing folder" picker (caption notes the path is shown in-app with a Copy button: home for Enrichment, records repo for My Brain) |
| 9 | `cowork-project-create.png` | Naming the project + **Create** |
| 10 | `cowork-scheduled-new.png` | **Routines/Scheduled → New → Local** |
| 11 | `cowork-scheduled-fields.png` | Routine form: name `mcpbrain-enrichment`, folder = Enrichment project, **Schedule = Hourly** |
| 12 | `cowork-run-now-allow.png` | **Run now** + an "Always allow" permission prompt |

Until a screenshot exists, the expander degrades to text-only (the `<img>` 404s and
is hidden via `onerror`), so the feature ships before the art is captured.

## Out of scope (explicit)

- Programmatic creation of the schedule **cadence / enabled state** (structurally
  impossible from an external process — see findings). The wizard guides the user
  to set it in Claude Desktop with screenshots instead.
- Programmatic **Cowork project registration** (writing `spaces.json`). We enrich
  the records-repo scaffold and point the user at the right folders; they register
  each space in-app.
- Capturing the screenshots themselves (maintainer action — see plan above). The
  feature ships text-only until the PNGs land.
- Windows live validation of the Cowork/ClickUp paths (no Windows machine;
  resolvers are defensive and string-tested).
