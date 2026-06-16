---
name: mcpbrain-install
description: Install and configure the mcpbrain brain. Run this in Claude Code (it installs the daemon directly on your Mac); then switch to Cowork for the project and scheduled tasks. Idempotent — safe to re-run.
---

# Install mcpbrain

**Run this skill in Claude Code** (the desktop app on your Mac), not in Cowork.
Claude Code runs on your machine, so it can install the daemon directly — run the
commands below yourself with the Bash tool. Cowork's sandbox can't write to the
home directory or register a background agent, which is why install happens here;
**Cowork is where you'll *use* the brain afterward** (Steps 6–8).

All recurring brain work (enrichment, gardening, meeting packs, reference gardening)
runs on your Claude subscription as Cowork Desktop Scheduled Tasks — no Anthropic API
and no background Claude CLI.

## Step 1 — Install the daemon (run these here in Claude Code)

```bash
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" mcpbrain --force
```

The `--python 3.12` pin is required — without it the install fails on machines whose
default Python is older.

## Step 2 — Register the background agent + open the wizard

```bash
mcpbrain setup
```

`mcpbrain setup` registers the background agent for the OS — **launchd** on macOS,
**Task Scheduler** on Windows — starts the daemon, and opens the setup wizard. Tell
the user to complete **Google sign-in, identity, and timezone** in the browser, then
confirm back here before continuing.

**Fleet (Centrepoint org):** the wizard's **Fleet setup** is pre-filled with the
Centrepoint `mcpbrain-fleet` and `mcpbrain-escrow` folder IDs — leave them as-is to
join the org fleet, or clear them if not part of the org.

**Backup:**
- **Fresh start:** click **Enable backup** in the wizard (generates a key, escrows it
  to the shared Drive, starts daily snapshots). The recovery path if you lose the Mac.
- **Recovering an existing brain (Step 3):** do **NOT** Enable backup — restore brings
  your original key and backup settings back; enabling here would mint a new key.

## Step 3 — Recover an existing brain (only if you've used mcpbrain before)

After Google sign-in, check for an existing backup and recover it:

```bash
mcpbrain restore --check
```

- **"No restorable backup found"** → fresh start; go to Step 4.
- **Restorable backup reported** → recover everything (store + records + config), key
  fetched automatically from the escrow folder:

  ```bash
  mcpbrain restore --auto
  ```

  Add `--force` if it reports the store already exists (safe on a fresh install). After
  a successful restore, **skip Step 4** — your world-model is back. Only the Google
  token isn't restored, and you just signed in.

## Step 4 — Bootstrap (fresh start only)

**Skip if you restored in Step 3.** Otherwise run the **`mcpbrain-bootstrap`** skill —
a one-time interview seeding your world-model (orgs, projects, systems, writing voice,
preferences) into your records repo.

## Step 5 — Open Claude at login

Tell the user: **set Claude to open at login** so the scheduled tasks fire.
- **macOS:** System Settings → General → Login Items → add Claude.
- **Windows:** Task Manager → Startup Apps → enable Claude.

Scheduled tasks run only while Claude is open and the machine is awake; a missed run is
caught up automatically on the next wake/reopen.

---

## Switch to Cowork for the rest

Steps 6–8 are Cowork-desktop features — do them in a **Cowork** session.

## Step 6 — Create the "My Brain" Cowork project

Get the working-folder path (run in Claude Code, or `mcpbrain home` anywhere):

```bash
mcpbrain home
```

In Cowork, create a project:
- **Project name:** `My Brain`
- **Working folder:** the exact path from `mcpbrain home`. This binds the project — the
  scheduled tasks in Step 7 point at the same path, so they run inside it.
- **Project instructions** (paste verbatim):

> You are working inside my personal brain. Use the mcpbrain tools (`brain_search`, `brain_actions`, `brain_context`, `brain_read`, `brain_note`, `brain_decision`) to ground every answer in what the brain already knows before responding. When I tell you something worth remembering, write it back with `brain_note` or `brain_memory_write`. Treat the working folder as my records repo — read CLAUDE.md and the records there for context.

## Step 7 — Create four Desktop Scheduled Tasks

In Cowork, create each with the **`/schedule` skill**, working folder = the `mcpbrain
home` path (binds them to the My Brain project). Cowork's schedule options are
**hourly, daily, weekly, on weekdays, or manually**:

| Task name | Schedule | Skill |
|---|---|---|
| `mcpbrain-enrich` | Hourly | `mcpbrain-enrich` |
| `mcpbrain-meeting-packs` | Hourly | `mcpbrain-meeting-packs` |
| `mcpbrain-gardener` | Weekly | `mcpbrain-gardener` |
| `mcpbrain-reference-gardener` | Weekly | `mcpbrain-reference-gardener` |

`mcpbrain-meeting-packs` runs **hourly** but is change-detecting — it rebuilds a pack
only when that meeting's context changed, so hourly is cheap. These tasks are
subscription-only (your Claude session) — no API key, no background CLI.

## Step 8 — Reload plugins

```
/reload-plugins
```

Connects the mcpbrain MCP server so `brain_search`, `brain_actions`, and the other
tools are available in Cowork.

## Done

The brain is configured. The hourly task starts enriching your email graph the next
time Claude is open. Run `brain_actions` in a few hours to see what it's learned.

## Idempotency

Every step is safe to re-run: `uv tool install` is a no-op at the same version, agent
registration is idempotent, the wizard skips filled fields, the bootstrap skill skips
already-seeded corpus files, and scheduled tasks can be deleted and recreated freely.
