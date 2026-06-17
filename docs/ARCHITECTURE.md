# mcpbrain — System Architecture

A one-page map of how the pieces fit together. For the *why* of distribution see
[`DISTRIBUTION.md`](DISTRIBUTION.md); for the maintainer release/rollout steps see
[`RELEASE-RUNBOOK.md`](RELEASE-RUNBOOK.md).

## What it is

mcpbrain is a **local-first personal brain**: a background daemon on the user's
own machine that syncs Gmail + Drive, embeds and graphs the content, and serves
it to Claude through an MCP server. Nothing about the brain's content leaves the
machine except the user's own encrypted backup to their own Drive.

## Components (all on the user's machine)

| Component | Entry point | Role |
|---|---|---|
| **Daemon** | `mcpbrain daemon` (`daemon.py`) | Long-running process started at login. Runs sync/enrich/embed/graph cycles, holds the store-writer lock, and self-updates (~daily) from the wheel index. |
| **Control API** | `control_api.py` (HTTP on `127.0.0.1:<port>`) | Serves the setup **wizard** and local control endpoints (e.g. `/api/backup/enable`, `/api/backup/auto`). The port is written to `<home>/control_port`. |
| **MCP server** | `mcpbrain mcp-server` (`mcp_server.py`, stdio) | What Claude connects to. Exposes the `brain_*` tools (search, enrich pull/push, …) and context resources. Reads the store **directly** (not via the daemon), so it answers even if the daemon is momentarily down. |
| **Tray** | `mcpbrain tray` | Optional menu-bar/status icon. Best-effort; the daemon runs without it. |
| **Store / home** | `config.app_dir()` | `~/Library/Application Support/mcpbrain` (macOS), `%APPDATA%\mcpbrain` (Windows), `~/.mcpbrain` (Linux). Contains `brain.sqlite3`, `config.json`, `control_port`, the records repo, and spool dirs. `MCPBRAIN_HOME` overrides it; an empty value falls back to the platform default. |

Login-agent registration (launchd on macOS, schtasks on Windows) and cadence
scheduling live in `agents.py`; `setup.py` wires it all up.

## How Claude connects to the brain (the connector)

The connector is **registered by `mcpbrain setup`**, not by the plugin's
`.mcp.json`. Setup runs:

```
claude mcp add mcpbrain --scope user -- <absolute-path-to-mcpbrain> mcp-server
```

The plugin's `plugin/.mcp.json` deliberately bundles **no** MCP server
(`"mcpServers": {}`); the `plugin/bin/mcpbrain-mcp` + `mcpbrain-monitor` shims
remain only as a documented manual fallback.

**Why registration instead of a bundled plugin server** — a static plugin
`.mcp.json` can't connect robustly on both macOS and Windows:

- MCP servers are spawned **shell-less on every OS** (confirmed in the Claude Code
  client), and `.mcp.json` has **no per-OS branching**.
- The documented macOS flow is **open-at-login**, so launchd hands Claude a
  minimal PATH that excludes `~/.local/bin` — a bare `mcpbrain` command wouldn't
  resolve, and an extensionless `#!/bin/sh` shim can't run on Windows at all.
- An earlier shim also injected `MCPBRAIN_HOME=~/.mcpbrain` (empty) instead of the
  real macOS home, so the connector attached to an empty store.

`mcpbrain setup` runs locally on each OS and knows the **resolved absolute** path
to the installed binary, so a single mechanism works identically on macOS and
Windows and points at the daemon's real home. User scope makes the `brain_*`
tools available in every Claude Code session, including scheduled tasks. Verify
with `claude mcp get mcpbrain` (expect **✔ Connected**, no `MCPBRAIN_HOME` in its
environment).

## The plugin

`Centrepoint-Church/mcpbrain-plugin` ships the **skills** (`mcpbrain-enrich`,
`mcpbrain-meeting-packs`, `mcpbrain-gardener`, `mcpbrain-reference-gardener`,
`mcpbrain-bootstrap`, `mcpbrain-backfill`, `mcpbrain-draft-reply`), hooks,
monitors, and the `INSTALL.md` prompt. The four recurring skills run as **Local**
scheduled tasks (Sonnet 4.6 + Auto permission mode) and do their work through the
`brain_*` MCP tools — so they need no working folder and no filesystem path.

## Distribution & update topology

Three repos under the **Centrepoint-Church** org:

- **`mcpbrain`** — source of truth (this repo).
- **`mcpbrain-dist`** — public PEP 503 wheel index on GitHub Pages
  (`…/mcpbrain-dist/simple/`). `update.py`'s `DEFAULT_INDEX_URL` points here, so a
  published bump auto-updates installed daemons within ~a day.
- **`mcpbrain-plugin`** — public plugin assets, distributed to staff via the org
  plugin marketplace.

Install is a single Claude Code session driven by the `plugin/INSTALL.md` prompt:
`uv tool install … mcpbrain` → `mcpbrain setup` (daemon + connector registration +
wizard). See [`RELEASE-RUNBOOK.md`](RELEASE-RUNBOOK.md) for the full release and
clean-machine validation procedure.

## Platform status

- **macOS** — supported; connector validated (`claude mcp get` → ✔ Connected).
- **Windows** — the daemon/tray schtasks generators are unit-tested and the
  setup-registered connector is cross-platform by design, but the live desktop
  flow (and a Windows-worded `INSTALL.md`) has **not** been validated on a real
  machine. See the Windows hard gate in [`RELEASE-RUNBOOK.md`](RELEASE-RUNBOOK.md).
