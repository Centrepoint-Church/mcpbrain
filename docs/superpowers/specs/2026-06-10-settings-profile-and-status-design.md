# Guided onboarding: settings/profile, MCP-registration status, Cowork projects & skill automation

**Date:** 2026-06-10
**Status:** design — awaiting user review

## Problem

Five gaps in the post-install experience, all in the daemon control API + wizard:

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
   find a List ID, set up the two Cowork projects (system vs daily-work), or attach
   the schedule to a project. Setup must walk through *every* element with
   real, anchored instructions + screenshots, and pre-create what it safely can.

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
| Cowork projects to seed | **Two local Cowork projects**: `mcpbrain Enrichment` (hosts the system scheduled task) and `My Brain` (the daily working space, seeded with instructions + context files) |
| Project registration | **Seed folders + READMEs only**; the user registers each as a Cowork space in-app (we do NOT write `spaces.json`) |

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

## Architecture

All work is in three existing layers (`probes.py`, `daemon.py`, `control_api.py`,
`wizard/index.html`) plus three small new modules (`timezones.py`,
`cowork_tasks.py`, `projects.py`) and package data (`cowork/enrichment.md`,
`wizard/img/`). No new third-party dependencies (`zoneinfo` is stdlib).

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
- `scheduled_dir() -> Path | None` — resolve the Cowork scheduled-tasks dir by
  trying, in order: `~/Documents/Claude/Scheduled`, `~/.claude/scheduled-tasks`
  (honouring `CLAUDE_CONFIG_DIR`). Returns the first whose *parent* exists (so we
  can create the task subdir), else `None`. Pure path logic; filesystem root
  injectable for tests.
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
- `apply_config` (existing) calls `cowork_tasks.write_enrichment_skill(home)` after
  a successful write so the SKILL.md is (re)materialised whenever the user saves
  settings. Failure to write degrades silently (logged at debug).

**`mcpbrain/control_api.py`**
- `do_GET`: add `GET /api/config` → `h_json(200, daemon.config_profile())` and
  `GET /api/timezones` → `h_json(200, {"zones": timezones.zone_options(now=datetime.now(timezone.utc))})`
  (the control API passes the current UTC time; the module computes each zone's
  offset against it). Both are token-guarded like every other control route.
- These are **one-shot reads**; they are deliberately NOT folded into
  `/api/status` (which the page polls every 3 s) so a poll can never overwrite
  what the user is typing.

**`mcpbrain/projects.py` (new, isolated, unit-tested) — Cowork project scaffolding**
- `projects_dir() -> Path | None` — resolve `~/Documents/Claude/Projects` (the
  Cowork local-projects root); `None` if its parent doesn't exist (degrade).
- `SEED_PROJECTS` — two entries:
  - `mcpbrain Enrichment` → folder + `README.md` explaining it hosts the background
    extraction scheduled task; the daemon drops `pending.json`, the task writes
    `enrich_inbox/`. (The scheduled task is created against THIS project's folder.)
  - `My Brain` → folder + seeded **context files** so Cowork approximates the
    Claude-Code experience: `README.md` (what this space is for), `INSTRUCTIONS.md`
    (how to use the mcpbrain MCP tools — `brain_search`, `brain_context`,
    `brain_actions`, `brain_draft_reply`, … — plus the user's identity/orgs pulled
    from config), and a `context/` folder pointer. Content is rendered from package
    data templates with the user's profile interpolated.
- `scaffold_projects(home) -> list[Path]` — create both folders + seed files
  idempotently (never overwrite a user-edited file: write only if absent). Returns
  created/seeded paths; degrades to `[]` if `projects_dir()` is `None`. Called from
  `apply_config` (best-effort, alongside the skill write).
- `project_status() -> dict` — for each seed project: `folder_present`,
  `registered` (best-effort: read `spaces.json` if discoverable and check a folder
  path match — read-only, degrade to `unknown`). Feeds a status card.

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

Each wizard step gets a short, always-visible plain-language explanation of *what
it is and why*, plus a **"Show me how" expander** revealing a screenshot + numbered
steps + Copy buttons. Written for someone with zero prior context. Steps:

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
5. **Your two Cowork projects** — a new step. Explains the two seeded projects and
   *what each is for*:
   - **mcpbrain Enrichment** — "the engine room: a scheduled task that quietly turns
     your mail into structured memory every hour." Expander: create a Cowork project
     from the seeded folder (Projects → + → Use an existing folder →
     `~/Documents/Claude/Projects/mcpbrain Enrichment`), then add an **Hourly**
     scheduled task pointed at the seeded `SKILL.md`. Screenshots for each click.
   - **My Brain** — "where you actually work with your brain." Expander: create a
     Cowork project from `~/Documents/Claude/Projects/My Brain`; the seeded
     `INSTRUCTIONS.md` already tells Claude how to use the mcpbrain tools. Screenshot.
   - A **"Create my project folders"** button calls a control endpoint that runs
     `projects.scaffold_projects()` and reports which folders were seeded, so the
     folders exist before the user opens Claude. Status card shows folder-present /
     registered per project.
6. **Status** — the configured home (already covered).

New control routes: `POST /api/projects/scaffold` → `scaffold_projects()`;
`project_status` is merged into `/api/status` connections (read-only, cheap).

## Data flow

```
page load ─► GET /api/timezones ─► populate <select>
        └─► GET /api/config (one-shot) ─► prefill fields (+ select saved tz)
        └─► every 3s: GET /api/status ─► renderStatus + renderHome (never touches form inputs)

Save ─► POST /api/config {non-blank fields} ─► write_config (merge)
                                            └─► cowork_tasks.write_enrichment_skill() [best-effort]

status poll ─► probes.all_connections ─► {google, claude(+registration), clickup, backup, records, enrichment}
```

## Error handling

- Every new probe and the Cowork writer **degrade, never raise** — a missing
  config file, an unwritable Cowork dir, or a moved directory yields a graceful
  state or a no-op, consistent with the existing probe contract.
- `GET /api/config` never includes the ClickUp secret (bool only).
- `zoneinfo` lookups are wrapped; an unknown curated zone is skipped, not fatal.

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
- `tests/test_projects.py` — `projects_dir` resolution/`None`; `scaffold_projects`
  creates both folders + seeds files, is idempotent, never overwrites a user-edited
  file, degrades to `[]` when unresolved; `project_status` read-only + degrades to
  `unknown`; seeded `INSTRUCTIONS.md` interpolates the profile.
- `tests/test_control_api.py` — `POST /api/projects/scaffold` returns seeded paths;
  `GET /img/<name>` serves a shipped PNG and 404s an unknown/sneaky name (path
  traversal rejected).

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
| 8 | `cowork-use-existing-folder.png` | "Use an existing folder" picker at `~/Documents/Claude/Projects/…` |
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
- Programmatic **Cowork project registration** (writing `spaces.json`). We seed
  folders + READMEs/instructions; the user registers each space in-app. Reading
  `spaces.json` for status is read-only + best-effort.
- Capturing the screenshots themselves (maintainer action — see plan above). The
  feature ships text-only until the PNGs land.
- Windows live validation of the Cowork/ClickUp paths (no Windows machine;
  resolvers are defensive and string-tested).
