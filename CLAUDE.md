# mcpbrain — project context for Claude Code

## This is a distributed PLUGIN, not just a local app

mcpbrain ships to other users as a **Claude Code plugin + a pip-installable package**.
There are **three repos** (all under the `Centrepoint-Church` org), and a change only
reaches users when the relevant ones are **pushed/released** — committing here and
running `uv tool install` only affects *this* machine.

| Repo | What it is | How users get it |
|---|---|---|
| **mcpbrain** (this repo) | Python package (`mcpbrain/`), the plugin source assets (`plugin/`), routines (`mcpbrain/routines/`), tests | source of truth; not installed directly |
| **mcpbrain-dist** (`../mcpbrain-dist`) | PEP 503 wheel index served by **GitHub Pages** | `pip`/`uv install mcpbrain` pulls the wheel from here |
| **mcpbrain-plugin** (`../mcpbrain-plugin`) | Public Claude Code plugin (agents/skills/hooks/commands/monitors), mirrored from this repo's `plugin/` | users add it as a Claude Code plugin |

`mcpbrain-dist` and `mcpbrain-plugin` are released **in lockstep** with the package
version. As of this note both lag the source (e.g. source may be at 0.7.5x while the
published wheel/plugin are at 0.7.41) — that gap is unshipped work.

## CRITICAL: local work ≠ shipped to users

- `git commit` in this repo → local only until `git push`.
- `uv tool install --force .` → updates **only this machine's** CLI + daemon.
- Restarting the daemon (`launchctl kickstart -k gui/$(id -u)/com.mcpbrain`) → this machine only.
- **None of the above changes what any other user installs.** Only the release steps below do.

Do **not** push or release without an explicit instruction — shipping is an all-users action.

## Releasing to prod (the full checklist)

Run from this repo unless noted. Steps 1–2 are usually already done per-commit.

1. **Bump the version** in all three: `mcpbrain/__init__.py`, `pyproject.toml`, and the
   `mcpbrain` entry in `uv.lock`. Keep them identical.
2. **If extraction rules changed** (`mcpbrain/enrich_prompt.md` SHARED-EXTRACTION-RULES
   block): run `python bin/sync_agents.py` so `plugin/agents/enrich-batch.md`'s embedded
   copy stays byte-identical (`test_enrich_agent_rules_in_sync` enforces this).
3. **Tests green**: `uv run pytest` (the suite gates the release).
4. **Push the source**: `git push` (origin = `Centrepoint-Church/mcpbrain`).
5. **Build + publish the wheel**:
   `python bin/release.py --dist ../mcpbrain-dist`
   → builds the wheel (`uv build --wheel`), copies it into `mcpbrain-dist/simple/mcpbrain/`,
   regenerates the PEP 503 `index.html`s. Then in `../mcpbrain-dist`: `git commit` + `git push`
   (GitHub Pages publishes it; `pip/uv install mcpbrain` now resolves the new version).
6. **If plugin assets changed** (`plugin/**` — agents, skills, hooks, commands, monitors —
   or `mcpbrain/routines/*.md`): mirror them into the `../mcpbrain-plugin` checkout, then
   `git commit` (message convention: `sync: …` / `release X.Y.Z: …`) + `git push`.
7. Verify `mcpbrain-dist` and `mcpbrain-plugin` are at the **same version** as the package.

## Caveats when deciding whether to ship

- Feature flags such as `salience_gate`, `schema_grounding`, `write_time_dedup` default to
  **OFF** in `config.py` — releasing the wheel does NOT activate them for users; they need
  config changes and should be validated on real data first.
- The wheel in `mcpbrain-dist` is the **only** thing a normal `mcpbrain self-update` / fresh
  install pulls — graph/enrichment logic changes are invisible to users until step 5 ships.
