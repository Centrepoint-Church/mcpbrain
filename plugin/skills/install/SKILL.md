---
name: mcpbrain-install
description: Install and fully configure the mcpbrain brain daemon — daemon, bootstrap interview, four Cowork scheduled tasks, and open-at-login instruction. Idempotent — safe to re-run.
---

# Install mcpbrain

Run this once in Cowork. If Cowork is running in full VM-sandbox mode (it cannot write to your home directory), run this skill in Claude Code instead, then return to Cowork.

All recurring brain work (enrichment, gardening, meeting packs, reference gardening) runs on your Claude subscription as Desktop Scheduled Tasks — no Anthropic API and no background Claude CLI.

## Steps

### 0. Check host access
```bash
touch ~/.local/.mcpbrain_probe 2>/dev/null && rm ~/.local/.mcpbrain_probe && echo HOST_OK || echo SANDBOX
```
If `SANDBOX`: stop and tell the user to run this skill in Claude Code, then return to Cowork. If `HOST_OK`: continue.

### 1. Install uv (if missing)
```bash
command -v uv || curl -LsSf https://astral.sh/uv/install.sh | sh
[ -f "$HOME/.local/bin/uv" ] && export PATH="$HOME/.local/bin:$PATH"
```

### 2. Install the mcpbrain daemon
```bash
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" mcpbrain --force
export PATH="$HOME/.local/bin:$PATH"
```

### 3. Register the background agent + wizard

`mcpbrain setup` registers the right background agent for your OS — **launchd** on macOS, **Task Scheduler** on Windows — installs the periodic cadences (records prune + health), starts the daemon, and opens the setup wizard:

```bash
mcpbrain setup
```

Complete Google sign-in, identity, and timezone in the wizard.

**Enable backup:** In the wizard, click **Enable backup**. This generates an encryption key, escrows a copy to the shared Drive folder, and starts hourly encrypted snapshots. Strongly recommended — it is the recovery path if you lose this machine.

**Fleet (Centrepoint org):** The wizard's **Fleet setup** section is pre-filled with the Centrepoint `mcpbrain-fleet` and `mcpbrain-escrow` folder IDs. Leave them as-is to join the org fleet (your install writes an hourly health beacon the maintainer can see in the fleet status report), or clear them if you're not part of the org. The escrow folder ID is also where your backup key is stored, so leave it set if you enable backup.

### 4. Create the "My Brain" Cowork project

**Project creation is a manual Cowork step by design** — the project and its instructions live in the Cowork desktop app's database, which plugins cannot register. Do this once by hand; it does not need re-investigating.

First resolve your brain home path — you will paste it as the project's working folder:

```bash
mcpbrain home
```

This prints an absolute path, e.g. `/Users/yourname/Library/Application Support/mcpbrain` (the folder is created during setup; this just shows you where it is). In Cowork, create a new project:

- **Project name:** `My Brain`
- **Working folder:** paste the exact path printed by `mcpbrain home` above. This working folder is what binds the project — the four scheduled tasks in step 7 point at the same path, so they run inside this project automatically.
- **Project instructions** (paste verbatim):

> You are working inside my personal brain. Use the mcpbrain tools (`brain_search`, `brain_actions`, `brain_context`, `brain_read`, `brain_note`, `brain_decision`) to ground every answer in what the brain already knows before responding. When I tell you something worth remembering, write it back with `brain_note` or `brain_memory_write`. Treat the working folder as my records repo — read CLAUDE.md and the records there for context.

All recurring brain work runs as Cowork Desktop Scheduled Tasks on your Claude subscription — no Anthropic API and no background Claude CLI.

### 5. Run the bootstrap interview

Run the **`mcpbrain-bootstrap`** skill. This is a one-time interview that seeds your initial world-model: your orgs, projects, systems, writing voice, and working preferences. It writes the answers into your records repo so the brain understands context from day one.

### 6. Open Claude at login

Tell the user: **Set Claude to open at login so your scheduled tasks run automatically.**

- **macOS:** System Settings → General → Login Items → add Claude.
- **Windows:** Task Manager → Startup Apps → enable Claude.

Note: Cowork Desktop Scheduled Tasks run only while Claude is open and the machine is awake. Opening at login ensures the hourly enrichment task fires each morning.

### 7. Create four Desktop Scheduled Tasks

Create each task with the **`/schedule` skill** — the first-class way to make a Cowork
scheduled task: type `/schedule` in the Cowork chat input and follow its prompts. (The
Scheduled tasks page — left sidebar → **Scheduled** → **+ New task** — is the equivalent
manual path if you prefer the form.)

For every task set the **working folder** to your brain home path:

```bash
mcpbrain home
```

Show the user the output (e.g. `/Users/yourname/Library/Application Support/mcpbrain`).
The working folder **is** the Cowork project the task runs in — pointing all four tasks at
this path binds them to your `My Brain` project (and creates that project if it doesn't
exist yet), so they run with the brain's MCP tools and records context.

Create these four tasks. The schedule options Cowork offers are **hourly, daily, weekly,
on weekdays, or manually** — use exactly these (pick any day/time when prompted for the
weekly ones):

| Task name | Schedule | Skill |
|---|---|---|
| `mcpbrain-enrich` | Hourly | `mcpbrain-enrich` |
| `mcpbrain-meeting-packs` | Hourly | `mcpbrain-meeting-packs` |
| `mcpbrain-gardener` | Weekly | `mcpbrain-gardener` |
| `mcpbrain-reference-gardener` | Weekly | `mcpbrain-reference-gardener` |

`mcpbrain-meeting-packs` runs **hourly** but is change-detecting — it only rebuilds a
meeting pack when that meeting's context has actually changed, so the hourly cadence is
cheap and packs refresh as soon as new relevant context lands.

These tasks are **subscription-only** — they run in your Claude session. No API key, no
background CLI process.

**Catch-up:** a scheduled task only fires while your computer is awake and the Claude
Desktop app is open. If a run is missed (asleep / app closed), Cowork runs it
automatically once you wake the machine or reopen the app — a skipped run is caught up,
not lost.

### 8. Reload plugins

```
/reload-plugins
```

This connects the mcpbrain MCP server so `brain_search`, `brain_actions`, and the other tools are available in Cowork.

## Done

The brain is now fully configured. The hourly task will start enriching your email graph the next time Claude is open. Check back in a few hours — run `brain_actions` to see what the brain has learned.

## Idempotency

Each step is safe to re-run: `uv tool install` is a no-op at the same version, agent registration is idempotent, the wizard skips already-filled fields, the bootstrap skill skips already-seeded corpus files, and scheduled tasks can be reviewed/deleted and recreated without side effects.
