# Settings/profile page, MCP-registration status & Cowork skill automation

**Date:** 2026-06-10
**Status:** design — awaiting user review

## Problem

Three gaps in the post-install experience, all in the daemon control API + wizard:

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

## Architecture

All work is in three existing layers plus two small new modules. No new
third-party dependencies (`zoneinfo` is stdlib).

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

## Out of scope (explicit)

- Programmatic creation of the schedule cadence / enabled state (structurally
  impossible from an external process — see findings).
- A one-line "ask Claude" trigger paste (user chose status-only scope).
- Windows live validation of the Cowork path (no Windows machine; resolver is
  defensive and string-tested).
