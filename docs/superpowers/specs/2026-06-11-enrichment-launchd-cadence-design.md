# Enrichment as a 30-minute daemon cadence via headless Claude Code

**Date:** 2026-06-11
**Status:** approved in conversation — building now
**Supersedes:** the Cowork enrichment path (`/mcpbrain-setup` + the `mcpbrain-setup`/`mcpbrain-enrichment` personal skills) from `2026-06-11-cowork-skills-and-mcp-context-design.md`. Extends the launchd-headless-cadence model from `~/joshbrain/.../2026-06-09-cowork-memory-architecture-design.md` to enrichment (which that spec had left as the lone Cowork exception).

## Problem

Enrichment was the only cadence still tied to Claude Cowork. That caused all the friction: a manual `/mcpbrain-setup` step, an "app must be open" requirement, the Cowork scheduled-task permission bug, and an enrichment status that could never reliably go green (the daemon deletes `enrich_inbox/*.json` within ~a minute of applying it, so the probe's "recent inbox file" signal almost never holds — a real bug). Meanwhile the gardener, meeting-packs, prune and health cadences already run headlessly via the local **Claude Code CLI (`claude -p`)** on launchd/systemd/schtasks timers, with no Cowork involvement.

## Decision

Run enrichment exactly like the gardener: a **30-minute** background cadence that invokes headless Claude Code on the shipped enrichment prompt. No Cowork, no manual setup, runs whether or not any app is open, $0 (uses the user's Claude Code subscription). **Install Claude Code as part of setup**, since enrichment now depends on it.

## Design — reuse the existing backfill machinery (don't rebuild)

`enrich_backfill.py` already is the headless-Claude-Code enricher: `local_claude_runner`
(shells to `claude -p`), and `run_backfill(store, embedder, …)` which loops
`prepare` (write `pending.json`) → `extractor_driver.run_extractor` (local claude →
`enrich_inbox`) → `drain` (apply + mark), gated on `is_configured`, stopping when no
threads remain. The daemon already runs it single-flight + threaded via
`start_enrich_backfill()` (guarded by `self._backfill_lock`). So "ongoing enrichment
via Claude Code" is just **that same loop kicked on a 30-minute daemon cadence** —
no separate process, no `run_cowork`, no new CLI, and no lock contention (it runs
inside the daemon that owns the single-writer lock). The old "catch-up only"
distinction dissolves: one enrichment path, triggered by the manual "Enrich history"
button **and** the 30-min cadence.

### 1. Daemon enrichment cadence (`mcpbrain/daemon.py`)
- New cadence interval `enrich_interval_s` (default **1800**), wired like the other
  `*_interval_s` cadences: `Daemon.__init__` arg, `_cadences_from_config`, and
  `apply_config`'s re-wire block, plus a `_last_enrich` anchor.
- New `maybe_enrich()` called from the run loop (next to the other `maybe_*` calls):
  if `enrich_interval_s` elapsed since `_last_enrich`, and `is_configured`, and not
  paused → kick the existing `start_enrich_backfill()` (single-flight: if a manual or
  prior cadence backfill is still running, it's a no-op). Always update `_last_enrich`
  and append a heartbeat line to `logs/enrich.log` so the probe has a durable
  "cadence fired" signal independent of the transient `enrich_inbox` files.
- Reuses `enrich_backfill.run_backfill` unchanged (which already drains to dry and
  no-ops when nothing is pending — zero `claude` cost when caught up).

### 2. Probe (`mcpbrain/probes.py`)
Rewrite `probe_enrichment(home)` — durable, no transient-inbox dependency, no `skills`:
- `claude` CLI not found (via `draft._find_claude`, wrapped) → `needs_action`,
  "Install Claude Code — enrichment runs through it".
- `logs/enrich.log` modified within the last **70 minutes** (> the 30-min cadence) →
  `ok`, "Running (every 30 min)".
- else → `needs_action`, "Enrichment hasn't run yet — sign into Claude Code".

### 3. Retire the Cowork enrichment skills
The cadence runs `run_backfill` directly, so the `~/.claude/skills` skills are obsolete:
- Delete `mcpbrain/skills.py` + `tests/test_skills.py`.
- Remove `skills.write_personal_skills()` from `daemon.apply_config` and daemon startup.
- Remove the `skills` import + usage from `probes.py`.
- Keep `mcpbrain/cowork/enrichment.md` (still the prompt `extractor_driver` feeds claude).

### 6. Install Claude Code in setup (`install/`)
- `setup.sh` + `setup.command`: after the uv install, add
  `command -v claude >/dev/null 2>&1 || run sh -c 'curl -fsSL https://claude.ai/install.sh | bash'`.
- `setup.ps1`: add `if (-not (Get-Command claude -EA SilentlyContinue)) { irm https://claude.ai/install.ps1 | iex }`.
- Claude Code requires a Pro/Max/Team account; **sign-in is interactive and one-time** (out of scope to automate). The wizard surfaces it.

### 7. Wizard (`mcpbrain/wizard/index.html`)
- Replace the step-projects "2. Start enrichment (/mcpbrain-setup)" expander with: *"Enrichment runs automatically every 30 minutes in the background through Claude Code — nothing to set up. If status shows 'Install Claude Code', run `curl -fsSL https://claude.ai/install.sh | bash` and sign in once."* (Copy button for the command.)
- The `enrichment` connection card already reflects `probe_enrichment` (now durable), so status shows "Running (every 30 min)" or the install/sign-in hint.

## Cross-check (both repos)
- **joshbrain memory-architecture spec** explicitly endorses launchd headless `claude` for meeting-packs/gardener/prune and notes the Cowork scheduled-task bug — this brings enrichment into the same model, the consistent choice. No contradiction; it removes the documented exception.
- **MCP-resource context + "My Brain" project** (prior spec) are untouched.
- **Single-writer lock**: enrich is file-in/file-out (no DB), so the separate launchd process never contends with the daemon's lock. The daemon still writes `pending.json` (prepare) and drains `enrich_inbox` (apply) under its lock.

## Testing
- `tests/test_daemon*.py`: `maybe_enrich` kicks `start_enrich_backfill` only when the
  interval elapsed + `is_configured` + not paused; is single-flight (no double-kick
  while a backfill runs); updates `_last_enrich`; writes the `logs/enrich.log`
  heartbeat. `enrich_interval_s` flows through `_cadences_from_config`/`apply_config`.
  (Monkeypatch `start_enrich_backfill`/`run_backfill` so no real `claude` runs.)
- `tests/test_probes.py`: `probe_enrichment` — claude-missing → needs_action; fresh
  `logs/enrich.log` → ok "Running"; stale/absent → needs_action. (Replaces the old
  skills-based test.)
- A string check: `install/setup.sh` + `install/setup.ps1` install Claude Code
  (`claude.ai/install.sh` / `install.ps1`).
- `tests/test_wizard_serve.py`: the enrichment step reads "automatic"/"every 30
  minutes", no `/mcpbrain-setup`.
- Remove `tests/test_skills.py`; update `tests/test_daemon_profile.py` etc. for the
  removed skill writes.

## Out of scope
- Automating Claude Code **sign-in** (interactive, one-time, account-gated).
- The `enrich_inbox` drain/apply path (unchanged).
- The "My Brain" project + MCP resources (unchanged).
