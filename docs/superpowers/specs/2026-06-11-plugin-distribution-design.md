# Distribute mcpbrain as an org-managed Claude plugin + an install skill

**Date:** 2026-06-11
**Status:** approved in conversation — spec for review
**Supersedes the *Claude-facing* install** in `2026-06-09-mcpbrain-productization-design.md` (the wheel-index + `curl|sh` + `mcpbrain register` + hooks/skills writing). The wheel index stays — but only for the daemon; the Claude integration moves into a plugin.

## Goal

Make onboarding for a non-technical org member: **the plugin is already there (org-pushed, auto-updating) → run one install skill in Cowork → done.** No terminal, no `curl|sh`, no manual MCP registration. One org-managed plugin carries everything Claude-facing (MCP server, skills, agents, hooks, commands) plus an install skill that bootstraps the daemon onto the machine from GitHub.

## Verified facts (researched 2026-06-11)

- A Claude **plugin bundles** skills, subagents, hooks (`hooks/hooks.json`), an MCP server (`.mcp.json`), commands, a `bin/` (added to PATH while enabled), and `settings.json`. Manifest at `.claude-plugin/plugin.json`. Distributed via a **marketplace** (a git repo with `.claude-plugin/marketplace.json`); installed with `/plugin install <name>@<marketplace>` or the Cowork UI (Cowork → Customize → Plugins → Browse → Install).
- **Cowork honours plugin MCP servers, hooks, and subagents** — "hooks and sub-agents run only in Cowork"; "skills bundled in a plugin work across all three" (chat, Desktop, Cowork).
- **Org-managed**: on **Team/Enterprise**, an owner distributes plugins org-wide via a marketplace; plugins can be **auto-installed/required** and auto-update. (Centrepoint must be on Team/Enterprise for the auto-push; otherwise members add the marketplace + install once — still terminal-free.)
- **Cowork has host CLI-ops** on this machine (`lastSeenRequireCoworkFullVmSandbox` unset, sessions reference host paths) → a Cowork install skill's Bash can install `~/.local/bin/mcpbrain` + a `~/Library/LaunchAgents` daemon + run `launchctl`.

## The boundary: plugin vs daemon

A plugin carries the *Claude-facing* layer only. It **cannot** be the **daemon** — the persistent background service that syncs Gmail/Drive, owns the SQLite store, runs the control-API/wizard, cadences, and backups (a launchd service, not a per-session plugin component). So:
- **`uv tool install mcpbrain`** (from the GitHub dist index) still installs the daemon + CLI + launchd agent — but it's now run **by the plugin's install skill**, not a terminal `curl|sh`.
- **The plugin** registers the MCP server, skills, agents, hooks.

## Design

### 1. The `mcpbrain` plugin (new repo / marketplace)
Layout (a new public repo, e.g. `itsjoshuakemp/mcpbrain-plugin`, holding both the plugin and a `marketplace.json`):
```
mcpbrain-plugin/
  .claude-plugin/
    plugin.json                  # name "mcpbrain", version, description, author
    marketplace.json             # lists this plugin (so the repo is its own marketplace)
  .mcp.json                      # mcpbrain MCP server (see §2)
  bin/
    mcpbrain-mcp                 # shim: locate the installed mcpbrain + exec `mcpbrain mcp-server`
  skills/
    install/SKILL.md             # the bootstrap install skill (§3)
    backfill/SKILL.md            # the Cowork-subagent $0 backfill (§4)
  agents/
    enrich-batch.md              # the per-batch enrichment subagent (§4)
  hooks/
    hooks.json                   # SessionStart (prime) + SessionEnd (capture) — Cowork-only
  commands/                      # optional /mcpbrain status, etc.
```
`version` is bumped per release so org members get controlled updates; the marketplace is org-added (managed) so it auto-installs/updates.

### 2. MCP server via a PATH-proof shim
A plugin `.mcp.json` is static and `mcpbrain` may not be on Cowork's GUI PATH (the launchd-PATH problem `register.py` solved by resolving an absolute path). So `.mcp.json` runs a shim shipped in the plugin's `bin/` (on PATH while enabled):
```json
{ "mcpServers": { "mcpbrain": {
    "command": "${CLAUDE_PLUGIN_ROOT}/bin/mcpbrain-mcp",
    "env": { "MCPBRAIN_HOME": "" } } } }
```
`bin/mcpbrain-mcp` resolves the real binary (`~/.local/bin/mcpbrain`, `uv tool` path, or PATH), sets `MCPBRAIN_HOME` (default `~/.mcpbrain`), and `exec`s `mcpbrain mcp-server`. Before the install skill runs, the binary is absent → the MCP shows disconnected (harmless); after install + `/reload-plugins` it connects. The shim also writes the heartbeat path correctly (reuse `write_heartbeat`).

### 3. The install skill (`skills/install/SKILL.md`)
Run once in Cowork (or Claude Code). Body instructs Claude (with Bash) to bootstrap the machine, mirroring today's `setup.sh` but driven by the skill:
1. Detect host access: if it can't write `~/.local` (full-VM-sandbox Cowork), tell the user to run this skill in **Claude Code** instead, and stop.
2. `command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh`.
3. `uv tool install --python 3.12 --index "mcpbrain=<dist-index>" mcpbrain --force` (the existing GitHub Pages wheel index — daemon source of truth).
4. Register the launchd login agent + cadences (`mcpbrain` ships `agents.install_*`; the skill calls `mcpbrain` to do it) and start the daemon (`mcpbrain daemon --once` then load the agent).
5. Open the setup wizard (`mcpbrain setup`) for Google sign-in + identity/orgs/timezone.
6. `/reload-plugins` so the MCP server connects.
Idempotent (skip steps already done). It does NOT do `mcpbrain register` (claude_desktop_config) — the **plugin** provides the MCP now; this avoids a double registration.

### 4. Cowork-subagent backfill (the $0 catch-up) — `skills/backfill/SKILL.md` + `agents/enrich-batch.md`
Subscription, fresh-context-per-batch (the context-limit fix). The backfill skill orchestrates a loop; each batch runs in a fresh-context **subagent**:
- `agents/enrich-batch.md` — a subagent that reads `~/.mcpbrain/enrich_queue/pending.json`, applies the enrichment rules (the body of `cowork/enrichment.md`), writes `enrich_inbox/<batch>.json`, returns a one-line status. Fresh context per batch.
- `skills/backfill/SKILL.md` — loop: while the spool isn't dry, dispatch one `enrich-batch` subagent, then wait for the daemon to drain + prepare the next; stop after N empty checks; report progress. The parent's context stays small (status lines only).
- Daemon support: a control endpoint / fast-cycle so the daemon prepares+drains promptly while a backfill is active, and stamps `logs/enrich.log` on each drain (the durable signal `probe_enrichment` already reads).
- The existing **headless `parallel_backfill`/`fast_backfill`** stays as the opt-in *pay-for-speed* path (separate cost), unchanged.

### 5. Hooks (`hooks/hooks.json`)
Move the SessionStart/SessionEnd memory hooks into the plugin (they "run only in Cowork"). They call `mcpbrain session-start` / `session-end` (the daemon CLI). Retire `hooks.py`'s settings.json-writing install — the plugin manages them.

### 6. What's removed / repurposed
- `mcpbrain register` (`wizard/register.py`, edits `claude_desktop_config.json`) — **removed** from the flow; the plugin's `.mcp.json` registers the MCP. (Keep the function only if needed for non-plugin users.)
- `hooks.install_session_hooks` (settings.json) — **removed**; plugin `hooks/hooks.json` instead.
- `curl|sh` installers (`install/setup.*`) — **demoted** to "advanced/manual"; the primary path is plugin + install skill. (Keep them for power users / CI.)
- The daemon, wheel index, `bin/release.py` — **unchanged** (daemon distribution).

## Cross-check (both repos)
- **Productization spec**: distribution = wheel index + `curl|sh`. This spec keeps the wheel index for the daemon but moves the Claude-facing install into the plugin; the `curl|sh` becomes the install skill. No conflict — it's a cleaner front door.
- **Settings/onboarding spec**: the wizard (Google/identity/timezone) is unchanged and still opened by the install skill. The "My Brain" project + MCP-resource context are unchanged (the plugin's MCP server exposes them).
- **joshbrain memory-architecture**: hooks > MCP > instructions determinism preserved; hooks now ship in the plugin (still user-scope-equivalent, Cowork).
- **Enrichment cost decision**: ongoing + backfill stay on the subscription (Cowork subagents); headless `parallel_backfill` is opt-in paid. The plugin changes packaging, not the cost model.

## Testing
- `tests/test_plugin_manifest.py`: `.claude-plugin/plugin.json` + `marketplace.json` are valid JSON with required fields; `.mcp.json` points at the `bin/` shim; `hooks/hooks.json` declares SessionStart + SessionEnd.
- `tests/test_plugin_assets.py`: the `enrich-batch` agent body embeds the `cowork/enrichment.md` rules; the backfill skill orchestrates subagents + loops-until-dry; the install skill contains the daemon-bootstrap steps + the VM-sandbox fallback.
- `bin/mcpbrain-mcp` shim: resolves the binary, sets `MCPBRAIN_HOME`, execs `mcp-server`; degrades with a clear message if the binary is absent.
- Daemon: drain stamps `logs/enrich.log`; the fast-cycle-while-backfilling endpoint.
- Keep the suite green; `mcpbrain register`/`hooks.install` removal doesn't break remaining callers (grep + update).

## Out of scope / risks to verify at build
- **Local MCP server in Cowork**: confirm a plugin's local stdio MCP runs on the host in a Cowork session (the existing `claude_desktop_config` registration already works in the desktop app, so likely yes — verify with the shipped plugin).
- **Org auto-push** needs Centrepoint on Team/Enterprise; otherwise members add the marketplace + install once.
- **Full-VM-sandbox Cowork**: the install skill can't reach the host there → run it in Claude Code (handled in §3).
- Google OAuth + identity remain human steps (the install skill opens the wizard).
- Building the marketplace/plugin repo + submitting/managing it is a maintainer action (like the dist repo).
