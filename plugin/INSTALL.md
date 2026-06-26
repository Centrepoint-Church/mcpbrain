# Installing mcpbrain

mcpbrain installs a background daemon on your **Mac or Windows PC** and runs entirely on
your machine. Setup is the **`/mcpbrain:install`** command — run it in a **Claude Code
(Desktop)** session and follow along. It installs the daemon, connects the brain to Claude
Desktop, opens the sign-in wizard, and creates the recurring background tasks as **Local**
scheduled tasks.

> **The one thing that matters:** the recurring tasks must be **Local** scheduled tasks
> (Claude Code Desktop → Routines → New routine → **Local**), *not* **Cloud routines**.
> Cloud routines (what `/schedule` creates) run on Anthropic's servers from a fresh clone
> and **can't reach your local mcpbrain** — enrichment would silently do nothing. The
> `/mcpbrain:install` command is explicit about this.

---

## Normal install (plugin already installed)

If the mcpbrain plugin is installed (e.g. it's a required/default plugin in your org
marketplace, so it's already there), just run:

```
/mcpbrain:install
```

and follow the steps it walks you through (install → wizard → four Local tasks → open at
login). That's it.

## Cold start (no plugin yet)

`/mcpbrain:install` only exists once the plugin is installed. On a brand-new machine
without it, add the marketplace and install the plugin first, then run the command:

```bash
claude plugin marketplace add Centrepoint-Church/mcpbrain-plugin
claude plugin install mcpbrain@centrepoint-church
# then, in a Claude Code session:
# /mcpbrain:install
```

---

## macOS

The daemon registers as a launchd login agent (`~/Library/LaunchAgents/com.mcpbrain.plist`)
and logs to `$MCPBRAIN_HOME/com.mcpbrain.log` and `com.mcpbrain.err`. `mcpbrain setup`
handles registration; `launchctl list | grep mcpbrain` confirms the agent is loaded.

## Windows

The daemon registers as a **Windows Scheduled Task** (Task Scheduler → `mcpbrain`) that
fires at logon. It launches via a hidden-console VBScript shim
(`%APPDATA%\mcpbrain\agents\mcpbrain.vbs`), so **no console window appears at logon** —
that is expected behaviour, not a failure.

Verify the task is registered and started:
```powershell
schtasks /query | findstr mcpbrain
```

Logs are written to `%APPDATA%\mcpbrain\com.mcpbrain.log` (rotating, max 1 MB × 3 files).
If something goes wrong after logon, check that file first.

---

## After setup

The brain runs in the background and is available wherever the mcpbrain plugin is
connected. Use it day-to-day in **Cowork** or Claude Code: ask questions and the
`brain_*` tools ground answers in what the brain knows — no folder to attach, because the
brain is served through its MCP tools.
