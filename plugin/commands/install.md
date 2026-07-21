---
description: Install and set up mcpbrain — install the daemon, connect it to Claude Desktop, complete the wizard, and create the recurring Local tasks. Works on macOS and Windows.
---

Install and set up mcpbrain. Run this in **Claude Code** (it needs to run shell commands).

**1. Install.**

*macOS:*
```bash
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" "mcpbrain[daemon]" --force
mcpbrain setup
```

*Windows (PowerShell):*
```powershell
irm https://centrepoint-church.github.io/mcpbrain-dist/install.ps1 -OutFile "$env:TEMP\mcpbrain-install.ps1"
& "$env:TEMP\mcpbrain-install.ps1"
mcpbrain doctor
```

(Note: The installer makes system changes — installing uv, the x64 VC++ runtime, autostart configuration, and Claude Desktop config — so it requires approval to run and is incompatible with restricted/managed execution policies.)

On macOS, `mcpbrain setup` registers a launchd login agent, writes the `mcpbrain` MCP server into Claude Desktop's config, and opens a browser wizard. On Windows, `install.ps1` reviews the machine (architecture, Python, VC++ runtime, uv, Task Scheduler) and installs the correct arch-native version of anything missing, then verifies with `mcpbrain doctor`. The installer registers the background agent (schtasks on Windows), writes the `mcpbrain` MCP server into Claude Desktop's config, and opens a browser wizard.

**2. Finish the wizard.** Tell me to complete the browser wizard that just opened:
Google sign-in, my identity, and timezone. **Backup and recovery are automatic** — the
wizard restores my brain if a backup already exists for my account, or turns on encrypted
backup if it's a fresh start. Do not run any restore or bootstrap command yourself.

**3. Connect to Claude Desktop (one click).** Install the mcpbrain Desktop
Extension: download `mcpbrain.mcpb` from
https://centrepoint-church.github.io/mcpbrain-dist/mcpbrain.mcpb and double-click
it, or in Claude Desktop → Settings → Extensions → Install from file. This wires
the `brain_*` tools with no config edit and no quit/reopen.

If your Claude Desktop build has no Extensions pane, run `mcpbrain connect` (quit Claude first, then reopen) as the manual equivalent.

**Note for Windows:** the daemon runs with a **hidden console** — no visible window at
logon is expected, not a failure. Verify it is running with:
```powershell
schtasks /query /tn mcpbrain
```
Check logs at `%APPDATA%\mcpbrain\com.mcpbrain.log` if something seems wrong.

**4. Create four scheduled tasks.** Once I confirm the wizard is done, create these four
recurring **Local** tasks — do **not** use `/schedule` (that makes a cloud routine that
can't reach mcpbrain). Pick any trusted folder. Create **each** with **Model: Sonnet 4.6**
and **Permission mode: Auto** so it runs unattended:

| Task name | Schedule | Model | Permission mode | Instructions (the task's prompt) |
|---|---|---|---|---|
| `Brain — enrich (hourly)` | Hourly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `enrich` and follow the instructions it returns exactly. |
| `Brain — meeting packs (hourly)` | Hourly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `meeting-packs` and follow the instructions it returns exactly. |
| `Brain — gardener (weekly)` | Weekly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `gardener` and follow the instructions it returns exactly. |
| `Brain — reference gardener (weekly)` | Weekly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `reference-gardener` and follow the instructions it returns exactly. |

After creating each task, click **Run now** once to confirm it works.

**5. Run on startup.** Remind me to turn on **Claude → Settings → Desktop App →
General → "Run on startup"** so Claude launches at login and the Local scheduled
tasks actually fire.
