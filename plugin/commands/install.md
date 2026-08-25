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

On macOS, `mcpbrain setup` registers a launchd login agent, writes the `mcpbrain` MCP server into Claude Desktop's config, and opens a browser wizard. On Windows, `install.ps1` ensures uv and the **x64** Visual C++ runtime are present, then installs mcpbrain with an x64 Python — which runs natively on x64 machines and under Windows' transparent emulation on ARM64 (native-ARM64 isn't viable: several dependencies ship no ARM64 Windows wheels) — and verifies with `mcpbrain doctor`. The installer registers the background agent (schtasks on Windows), writes the `mcpbrain` MCP server into Claude Desktop's config, and opens a browser wizard.

**2. Finish the wizard.** Tell me to complete the browser wizard that just opened:
Google sign-in, my identity, and timezone. **Backup and recovery are automatic** — the
wizard restores my brain if a backup already exists for my account, or turns on encrypted
backup if it's a fresh start. Do not run any restore or bootstrap command yourself.

**3. Connect Claude Desktop (one click).** In the wizard that just opened, click
**"Connect & restart Claude Desktop"** as the LAST step of the wizard — it writes
the connector then quits and reopens Claude Desktop so it loads the `brain_*`
tools. Do this after the wizard's Google/profile/model steps, not before.

If that button can't restart Claude Desktop on your machine (the wizard will say
so), run `mcpbrain connect` yourself and then quit and reopen Claude Desktop by
hand.

**Note for Windows:** the daemon runs with a **hidden console** — no visible window at
logon is expected, not a failure. Verify it is running with:
```powershell
schtasks /query /tn mcpbrain
```
Check logs at `%APPDATA%\mcpbrain\com.mcpbrain.log` if something seems wrong.

**4. Create the four recurring tasks.** Once I confirm the wizard is done, **you
create these four Local scheduled tasks yourself** — do not ask me to build them
in the UI. They must be **Local** scheduled tasks. Never use `/schedule`: that
creates a cloud routine, which runs from a fresh clone on Anthropic's servers,
cannot reach my local mcpbrain daemon, and so silently does nothing forever.

**First, check what already exists.** List my current scheduled tasks. If any of
these four are already present, update them in place rather than creating a
second copy — a duplicate hourly enrich coordinator doubles Haiku spend
indefinitely and nothing surfaces it.

Create each with **Model: Sonnet 4.6**, **Permission mode: Auto** (so it runs
unattended), and any trusted folder as the working folder:

| Name | Schedule | Instructions (the task's prompt) |
|---|---|---|
| `brain-enrich-hourly` | Hourly | Call the `brain_routine` tool with name `enrich` and follow the instructions it returns exactly. |
| `brain-meeting-packs-hourly` | Hourly | Call the `brain_routine` tool with name `meeting-packs` and follow the instructions it returns exactly. |
| `brain-gardener-weekly` | Weekly | Call the `brain_routine` tool with name `gardener` and follow the instructions it returns exactly. |
| `brain-reference-gardener-weekly` | Weekly | Call the `brain_routine` tool with name `reference-gardener` and follow the instructions it returns exactly. |

Then **verify, don't assume**: list my scheduled tasks back and confirm all four
exist, are **Local**, and are Active. Report what you find.

Finally, click **Run now** on each once while I'm still here, so any permission
prompts get answered now rather than stalling an unattended 3am run.

If you cannot create scheduled tasks (Routines disabled by org policy, or an
older Desktop build), say so plainly and point me at the manual table in the
plugin's `INSTALL.md` — do not silently skip this step.

**5. Run on startup.** Remind me to turn on **Claude → Settings → Desktop App →
General → "Run on startup"** so Claude launches at login and the Local scheduled
tasks actually fire.
