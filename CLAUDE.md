# mcpbrain — project context for Claude Code

## This is a distributed PLUGIN, not just a local app

mcpbrain ships to other users as a **Claude Code plugin + a pip-installable package**.
There are **three repos** (all under the `Centrepoint-Church` org), and a change only
reaches users when the relevant ones are **pushed/released** — committing here and
running `uv tool install` only affects *this* machine.

| Repo | What it is | How users get it |
|---|---|---|
| **mcpbrain** (this repo) | Python package (`mcpbrain/`), plugin source assets (`plugin/`), routines (`mcpbrain/routines/`), tests | source of truth; not installed directly |
| **mcpbrain-dist** (`../mcpbrain-dist`) | PEP 503 wheel index on GitHub Pages (`centrepoint-church.github.io/mcpbrain-dist/simple/`) | `uv tool install --index` pulls the wheel; installed daemons **auto-update daily** from here (`update.py`) |
| **mcpbrain-plugin** (`../mcpbrain-plugin`) | Public Claude Code plugin (agents/skills/hooks/commands/monitors + `.claude-plugin/`), mirrored from this repo's `plugin/` | org **plugin marketplace** (Claude Team/Enterprise settings) |

## CRITICAL: local work ≠ shipped to users

- `git commit` here → local until `git push`. `uv tool install --force .` and
  `launchctl kickstart` → **this machine only.** Neither changes what any other user installs.
- Do **not** push or release without an explicit instruction — shipping is an all-users action.

## Releasing to prod

**`docs/RELEASE-RUNBOOK.md` is the authoritative, step-by-step procedure** (the *do*);
`docs/DISTRIBUTION.md` is the *why*. Follow the runbook. The things that are easy to get
wrong and MUST be right:

- **Version lives in FOUR files, keep them equal:** `pyproject.toml`, `mcpbrain/__init__.py`,
  `plugin/.claude-plugin/plugin.json`, `plugin/.claude-plugin/marketplace.json`.
  The two plugin manifests are the marketplace's version and are **easy to forget** —
  bumping only `__init__.py`/`pyproject.toml` ships a wrong plugin version. (`uv.lock`'s
  mcpbrain entry is also kept in step but isn't a marketplace source-of-truth.)
- Release = push `mcpbrain` source → `python bin/release.py --dist ../mcpbrain-dist` then
  commit+push `mcpbrain-dist` (mind the stale-wheel gotcha in the runbook) → sync `plugin/`
  into `mcpbrain-plugin` via `git archive HEAD:plugin` + push. All three repos end at the
  same version.
- If extraction rules changed, run `python bin/sync_agents.py` first (keeps
  `plugin/agents/enrich-batch.md` byte-identical to `mcpbrain/enrich_prompt.md`).

## Shipping caveats

- Some feature flags (`schema_grounding`, `write_time_dedup`) still default **OFF** in
  `config.py` — releasing the wheel does NOT activate them; they need config + real-data validation.
- The **Q1 salience gate (`salience_gate`) is the exception: validated on the live store
  (~40% of the corpus gated as tabular/low-signal with no recall impact) and flipped default
  **ON** in 0.7.65** (commit `cfe0338`). It ships active for all users. Source-aware
  `should_enrich()` in `prepare.py` cold-marks promotional email + tabular/short Drive docs
  before extraction; cold-marking is reversible (chunks stay embedded/searchable). The aggressive
  `salience_require_drive_mention` sub-flag remains opt-in OFF.
- **Current state (2026-06-30):** all four version files **and** the published wheel are at
  `0.7.70` — source, dist index, and plugin manifests are in step. The 0.7.42→0.7.70 line
  (embedder fix, Phase 0/1, OCR, validated brain layer ON, salience gate ON, Windows support)
  is shipped.
