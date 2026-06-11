# Distribute mcpbrain as an org-managed Claude plugin + an install skill

**Date:** 2026-06-11
**Status:** approved in conversation — spec for review
**Supersedes the *Claude-facing* install** in `2026-06-09-mcpbrain-productization-design.md` (the wheel-index + `curl|sh` + `mcpbrain register` + hooks/skills writing). The wheel index stays — but only for the daemon; the Claude integration moves into a plugin.

## Decisions (Josh, 2026-06-11)
1. **Team plan, Josh is Owner.** Plugin distribution = **available in the org marketplace, members click Install** (not force-auto-installed). Rollout = a **pilot** of the members who already have Claude.
2. **Marketplace repo = a dedicated *public* repo under the Centrepoint GitHub org** (public so members need no GitHub account; the plugin has no secrets). Move the daemon wheel-index repo there too.
3. **Move the distribution infra to Centrepoint** as part of this work: GitHub org repos (plugin + wheel index) **and** a Centrepoint Google Cloud project + its own desktop OAuth client (replacing the personal `itsjoshuakemp` infra). See §7.
4. **Remove** `curl|sh` installers **and** `mcpbrain register` entirely — the plugin + install skill is the only path (keep it clean).
5. **Monitoring ships in the plugin** (`monitors/`) — surface daemon/enrichment health in Cowork. See §8.
6. Per-user **Google OAuth test users** are added by Josh in the Console (NOT in the plugin/skill). The **fast headless backfill** stays documented as an opt-in paid power tool (no Claude Code auto-install). Plugin name stays **mcpbrain**. The "My Brain" working project stays a guided manual wizard step.

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
    mcpbrain-monitor             # shim: locate the installed mcpbrain + exec `mcpbrain monitor` (§8)
  skills/
    install/SKILL.md             # the bootstrap install skill (§3)
    backfill/SKILL.md            # the Cowork-subagent $0 backfill (§4)
  agents/
    enrich-batch.md              # the per-batch enrichment subagent (§4)
  hooks/
    hooks.json                   # SessionStart (prime) + SessionEnd (capture) — Cowork-only
  monitors/
    monitors.json                # daemon/enrichment/backup health surface — Cowork-only (§8)
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

### 6. What's removed (decision 4 — keep it clean)
The plugin + install skill is the **only** install path. Everything that duplicated it is deleted, not demoted. Each removal has a **cascade** of now-dead callers — delete the whole chain, not just the entry point:

**MCP registration (plugin `.mcp.json` replaces it):**
- `mcpbrain register` subcommand (`cli.py`) + `wizard/register.py` (edits `claude_desktop_config.json`) — **deleted**.
- `Daemon.register()` (`daemon.py`) + the `/api/register` route (`control_api.py`) — dead once `register_mcpbrain` is gone.
- Wizard **`#step-register`** card + its `reg()` JS (`wizard/index.html`).
- `probes.probe_claude` no longer imports `claude_desktop_config_path` / `_claude_registered`; the Claude probe keys off the MCP heartbeat alone (the registered-but-no-heartbeat state is dropped).

**Hook installation (plugin `hooks/hooks.json` replaces the *installer*, not the executor):**
- `hooks.py` `install_session_hooks` / `uninstall_session_hooks` / `hooks_status` + private helpers (settings.json writer) — **deleted**.
- The `/api/hooks/install` route (`control_api.py`) + wizard **`#step-hooks`** card + its `installHooks()` JS.
- `probes.probe_memory_hooks` — the plugin always provides the hooks, so the probe is removed (or hard-coded "on") rather than reading `hooks_status`.
- **Kept:** `session_hooks.py` and the `mcpbrain session-start` / `session-end` CLI subcommands — the plugin's `hooks/hooks.json` *invokes* these (§5). Only the settings.json-writing installer goes.

**Installer scripts:**
- `curl|sh` installers (`install/setup.sh|.command|.ps1`) — **deleted**. No "advanced/manual" fallback; the install skill (which can also run in Claude Code, §3) covers the non-Cowork case.
- **Kept:** `mcpbrain setup` / `setup.py` — the install skill opens it for Google sign-in + identity/timezone (§3 step 5).

**Redundant third enrichment path:**
- `bin/drain_backlog.py` — **deleted**. A serial-polling headless `claude --print` drainer that predates and is functionally dominated by `parallel_backfill`/`fast_backfill` (no tests; carries the load-time non-packaged-import hazard). `fast_backfill.py` is the one supported opt-in headless path; the Cowork-subagent backfill (§4) is the $0 steady-state path. Update the `extractor_io.py` module docstring that still calls `drain_backlog` a co-equal path.

**Unchanged:** the daemon, wheel index, `bin/release.py` (daemon distribution); `extractor_io` / `extractor_driver` / `parallel_backfill` / `enrich_backfill` (all live, distinct roles).

**Test/grep sweep:** delete `test_register.py`, `test_hooks.py`, and the register/hooks-install assertions in `test_control_api_post.py`, `test_control_api_actions.py`, `test_wizard_serve.py`, `test_probes.py` so the suite stays green; grep confirms no dangling import after each removal.

## 7. Infra migration to Centrepoint (decision 3)
Move distribution off the personal `itsjoshuakemp` accounts onto Centrepoint-owned infra, as part of this work:
- **GitHub**: a Centrepoint GitHub org with two **public** repos — `centrepoint/mcpbrain-plugin` (plugin + `marketplace.json`) and `centrepoint/mcpbrain-dist` (the PEP 503 wheel index, today on `itsjoshuakemp.github.io`). Public so members need no GitHub account and no secrets are exposed (decision 2/7).
- **Google Cloud**: a Centrepoint GCP project with its **own** OAuth consent screen + **desktop OAuth client**, replacing the personal client. The new client ID/secret is bundled in the daemon wheel (as today) so sign-in is keyless for the member. Josh adds OAuth test users in the Console manually (decision 6) until the consent screen is verified.
- **Cutover**: bump the wheel-index URL in the install skill + `bin/release.py` publish target to the Centrepoint Pages URL; bump the marketplace URL members add. Old personal repos can be archived once the pilot is on the new infra.
- This is a maintainer/owner action (like standing up the original dist repo), not something the plugin or skill automates.

## 8. Monitoring in the plugin (decision 5)
Surface daemon/enrichment health inside Cowork so a non-technical member sees problems without opening the wizard:
- The plugin ships `monitors/monitors.json` declaring one monitor that runs `${CLAUDE_PLUGIN_ROOT}/bin/mcpbrain-monitor` (a PATH-proof shim like `mcpbrain-mcp`) which calls a new `mcpbrain monitor` CLI.
- `mcpbrain monitor` reads local state only (no network) and emits a compact health line + non-zero exit on trouble: **daemon down** (no recent heartbeat / launchd agent not loaded), **sync error** (last cycle errored in `logs/`), **enrichment idle** (the `probe_enrichment` durable signal stale), **backup stale** (`probe_backup` needs_action). It reuses `probes.all_connections` so the monitor and wizard never disagree.
- Cowork renders the monitor output as a status surface/notification (monitors "run only in Cowork", like hooks). No new daemon endpoint — the CLI reads the same files the wizard polls.
- Tests: `mcpbrain monitor` returns ok/exit-0 on a healthy home and a clear message/exit-1 per failure mode; `monitors/monitors.json` is valid and points at the shim.

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
- `mcpbrain monitor` (§8): exit-0 + "ok" on a healthy home; exit-1 + a specific message for each failure mode (daemon down, sync error, enrichment idle, backup stale); `monitors/monitors.json` is valid JSON pointing at the `bin/mcpbrain-monitor` shim.
- Keep the suite green after the §6 deletions: removing `mcpbrain register`/`wizard/register.py`/`hooks.install_session_hooks`/`install/setup.*` leaves no dangling import or test (grep + update `probes.py`, `test_probes.py`, `test_wizard_serve.py`).

## Out of scope / risks to verify at build
- **Local MCP server in Cowork**: confirm a plugin's local stdio MCP runs on the host in a Cowork session (the existing `claude_desktop_config` registration already works in the desktop app, so likely yes — verify with the shipped plugin).
- **Org auto-push** needs Centrepoint on Team/Enterprise; otherwise members add the marketplace + install once.
- **Full-VM-sandbox Cowork**: the install skill can't reach the host there → run it in Claude Code (handled in §3).
- Google OAuth + identity remain human steps (the install skill opens the wizard).
- Building the marketplace/plugin repo + submitting/managing it is a maintainer action (like the dist repo).
