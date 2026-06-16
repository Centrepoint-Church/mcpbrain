---
name: mcpbrain-install
description: Install and fully configure the mcpbrain brain — guides the user to paste one install script into Terminal, then sets up restore/bootstrap, the Cowork project, and four scheduled tasks. Idempotent — safe to re-run.
---

# Install mcpbrain

Run this skill in Cowork. It **guides** the install — you don't run host commands
from here. The mcpbrain daemon installs on the real machine (a launchd/Task
Scheduler background agent, files under your home directory), which Cowork's
sandbox can't touch, so the daemon install is done by **pasting one script into
Terminal**. Everything after that is the wizard plus a few Cowork-app steps.

All recurring brain work (enrichment, gardening, meeting packs, reference
gardening) runs on your Claude subscription as Desktop Scheduled Tasks — no
Anthropic API and no background Claude CLI.

## Step 1 — Install the daemon (paste into Terminal)

Tell the user to open **Terminal** (macOS) or **PowerShell** (Windows) and paste the
whole block. It installs `uv` if missing, installs mcpbrain, then registers the
background agent — **launchd** on macOS, **Task Scheduler** on Windows — and opens
the setup wizard in the browser.

**macOS (Terminal):**
```bash
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" mcpbrain --force
mcpbrain setup
```

**Windows (PowerShell):**
```powershell
if (-not (Get-Command uv -ErrorAction SilentlyContinue)) { irm https://astral.sh/uv/install.ps1 | iex }
uv tool install --python 3.12 --index "mcpbrain=https://centrepoint-church.github.io/mcpbrain-dist/simple/" mcpbrain --force
mcpbrain setup
```

The `--python 3.12` pin is required — without it the install fails on machines whose
default Python is older. `mcpbrain setup` is idempotent: safe to re-run.

## Step 2 — Complete the wizard

`mcpbrain setup` opens the wizard. Complete **Google sign-in, identity, and timezone**.

**Fleet (Centrepoint org):** the **Fleet setup** section is pre-filled with the
Centrepoint `mcpbrain-fleet` and `mcpbrain-escrow` folder IDs — leave them as-is to
join the org fleet (your install writes an hourly health beacon the maintainer can
see), or clear them if you're not part of the org.

**Backup:**
- **Fresh start (no prior brain):** click **Enable backup** in the wizard. It
  generates an encryption key, escrows a copy to the shared Drive, and starts daily
  encrypted snapshots. Strongly recommended — it's the recovery path if you lose this
  machine.
- **Recovering an existing brain (see Step 3):** do **NOT** click Enable backup — the
  restore brings your original key and backup settings back. Enabling here would mint
  a new key.

## Step 3 — Recover an existing brain (only if you've used mcpbrain before)

Right after Google sign-in, check whether this account already has a backup on the
Shared Drive (reinstalling, or a new machine). In the **same Terminal**:

```bash
mcpbrain restore --check
```

- **"No restorable backup found"** → fresh start; continue to Step 4.
- **A restorable backup is reported** → recover everything (store + records +
  config), with the decryption key fetched automatically from the escrow folder:

  ```bash
  mcpbrain restore --auto
  ```

  Add `--force` if it reports the store already exists (safe on a fresh install — the
  daemon may have created an empty one). After a successful restore, **skip Step 4
  (bootstrap)** — your world-model is already back. Only the Google token isn't
  restored, and you just signed in, so you're set.

## Step 4 — Bootstrap (fresh start only)

**Skip if you restored in Step 3.** Otherwise run the **`mcpbrain-bootstrap`** skill —
a one-time interview that seeds your world-model (orgs, projects, systems, writing
voice, preferences) into your records repo.

## Step 5 — Open Claude at login

Tell the user: **set Claude to open at login** so the scheduled tasks fire.
- **macOS:** System Settings → General → Login Items → add Claude.
- **Windows:** Task Manager → Startup Apps → enable Claude.

Scheduled tasks run only while Claude is open and the machine is awake; a missed run
is caught up automatically on the next wake/reopen.

## Step 6 — Create the "My Brain" Cowork project

Project creation is a manual Cowork step by design. First get the working-folder path
(have the user run this in the same Terminal and copy the output):

```bash
mcpbrain home
```

In Cowork, create a project:
- **Project name:** `My Brain`
- **Working folder:** the exact path from `mcpbrain home`. This working folder binds
  the project — the scheduled tasks in Step 7 point at the same path, so they run
  inside this project.
- **Project instructions** (paste verbatim):

> You are working inside my personal brain. Use the mcpbrain tools (`brain_search`, `brain_actions`, `brain_context`, `brain_read`, `brain_note`, `brain_decision`) to ground every answer in what the brain already knows before responding. When I tell you something worth remembering, write it back with `brain_note` or `brain_memory_write`. Treat the working folder as my records repo — read CLAUDE.md and the records there for context.

## Step 7 — Create four Desktop Scheduled Tasks

Create each with the **`/schedule` skill** (type `/schedule` in Cowork), working folder
= the `mcpbrain home` path (this binds them to the My Brain project). Cowork's schedule
options are **hourly, daily, weekly, on weekdays, or manually** — use those:

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
