# Installing mcpbrain

mcpbrain installs a background daemon on your **Mac** and runs entirely on your machine.
Everything is set up from **one Claude Code session** — paste the prompt below. It
installs the daemon, opens a browser wizard for sign-in, and creates the recurring
background tasks as **Local** scheduled tasks.

> **The one thing that matters:** the recurring tasks must be **Local** scheduled tasks
> (Claude Code Desktop → Routines → New routine → **Local**), *not* **Cloud routines**.
> Cloud routines (what `/schedule` creates) run on Anthropic's servers from a fresh clone
> and **can't reach your local mcpbrain** — enrichment would silently do nothing. The
> prompt below is explicit about this.

---

## Paste this into a Claude Code (Desktop) session

> Install and set up mcpbrain on my Mac.
>
> **1. Install.** Run these with the Bash tool:
>
> ```bash
> command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
> export PATH="$HOME/.local/bin:$PATH"
> uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" mcpbrain --force
> mcpbrain setup
> ```
>
> `mcpbrain setup` registers the background agent (launchd), **connects the brain to
> Claude Desktop automatically** (it writes the `mcpbrain` MCP server into
> `claude_desktop_config.json` with the absolute install path, so the `brain_*` tools
> appear after a Desktop restart), and opens a browser wizard.
>
> **2. Finish the wizard.** Tell me to complete the browser wizard that just opened:
> Google sign-in, my identity, and timezone. **Backup and recovery are automatic** — the
> wizard restores my brain if a backup already exists for my account, or turns on encrypted
> backup if it's a fresh start. Do not run any restore or bootstrap command yourself.
>
> **3. Create four scheduled tasks.** Once I confirm the wizard is done, create these four
> recurring tasks (asking you to create a scheduled task here makes a **Local** task that
> runs on this machine — that's what we want; do **not** use `/schedule`, which makes a
> cloud routine that can't reach mcpbrain). The tasks do all their work through the
> **mcpbrain MCP tools**, so the working folder doesn't matter — pick any trusted folder.
> The `brain_*` tools were connected by `mcpbrain setup`; if they aren't visible yet,
> restart the app (or run `/reload-plugins`). Create **each** task with **Model: Sonnet 4.6**
> and **Permission mode: Auto** so it runs unattended without stopping to ask for approval.
>
> Each task's prompt tells Claude to fetch its instructions from the **`brain_routine`
> MCP tool** and follow them. The protocol is returned by the tool (served by the daemon
> and auto-updated with it), so the task depends only on the `brain_*` MCP tools — nothing
> to "look up" as a skill or command (skill/command resolution is unreliable in a
> scheduled run; MCP tools are not):
>
> | Task name | Schedule | Model | Permission mode | Instructions (the task's prompt) |
> |---|---|---|---|---|
> | `Brain — enrich (hourly)` | Hourly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `enrich` and follow the instructions it returns exactly. |
> | `Brain — meeting packs (hourly)` | Hourly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `meeting-packs` and follow the instructions it returns exactly. |
> | `Brain — gardener (weekly)` | Weekly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `gardener` and follow the instructions it returns exactly. |
> | `Brain — reference gardener (weekly)` | Weekly | Sonnet 4.6 | Auto | Call the `brain_routine` tool with name `reference-gardener` and follow the instructions it returns exactly. |
>
> (`mcpbrain` is the plugin name, so the commands are namespaced `/mcpbrain:…`. After
> creating each task, click **Run now** once to confirm it works. The meeting-packs
> command is change-detecting, so hourly is cheap.)
>
> **4. Open at login.** Remind me to set Claude to open at login (System Settings → General
> → Login Items → add Claude) so the Local tasks actually fire.

---

## After setup

The brain runs in the background and is available wherever the mcpbrain plugin is
connected. Use it day-to-day in **Cowork** or Claude Code: ask questions and the
`brain_*` tools ground answers in what the brain knows — no folder to attach, because the
brain is served through its MCP tools.
