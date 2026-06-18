---
description: Install and set up mcpbrain on this Mac — install the daemon, connect it to Claude Desktop, complete the wizard, and create the recurring Local tasks.
---

Install and set up mcpbrain on my Mac. Run this in **Claude Code** (it needs to run
shell commands).

**1. Install.** Run these with the Bash tool:

```bash
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" mcpbrain --force
mcpbrain setup
```

`mcpbrain setup` registers the background agent (launchd), **connects the brain to
Claude Desktop automatically** (it writes the `mcpbrain` MCP server into
`claude_desktop_config.json` with the absolute install path, so the `brain_*` tools
appear after a Desktop restart), and opens a browser wizard.

**2. Finish the wizard.** Tell me to complete the browser wizard that just opened:
Google sign-in, my identity, and timezone. **Backup and recovery are automatic** — the
wizard restores my brain if a backup already exists for my account, or turns on encrypted
backup if it's a fresh start. Do not run any restore or bootstrap command yourself.

**3. Create four scheduled tasks.** Once I confirm the wizard is done, create these four
recurring tasks (asking you to create a scheduled task here makes a **Local** task that
runs on this machine — that's what we want; do **not** use `/schedule`, which makes a
cloud routine that can't reach mcpbrain). The tasks do all their work through the
**mcpbrain MCP tools**, so the working folder doesn't matter — pick any trusted folder.
Create **each** task with **Model: Sonnet 4.6** and **Permission mode: Auto** so it runs
unattended without stopping to ask for approval. Each task's prompt tells Claude to fetch
its instructions from the **`brain_routine` MCP tool** and follow them (the protocol is
served by the daemon and auto-updated with it, so the task depends only on the `brain_*`
MCP tools — nothing to look up as a skill or command):

| Task name | Schedule | Model | Permission mode | Instructions (the task's prompt) |
|---|---|---|---|---|
| `Brain — enrich (hourly)` | Hourly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `enrich` and follow the instructions it returns exactly. |
| `Brain — meeting packs (hourly)` | Hourly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `meeting-packs` and follow the instructions it returns exactly. |
| `Brain — gardener (weekly)` | Weekly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `gardener` and follow the instructions it returns exactly. |
| `Brain — reference gardener (weekly)` | Weekly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `reference-gardener` and follow the instructions it returns exactly. |

After creating each task, click **Run now** once to confirm it works. The meeting-packs
task is change-detecting, so hourly is cheap.

**4. Run on startup.** Remind me to turn on **Claude → Settings → Desktop App →
General → "Run on startup"** so Claude launches at login and the Local scheduled
tasks actually fire.
