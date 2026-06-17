---
name: mcpbrain-install
description: Install the mcpbrain brain on this Mac. Run this in Claude Code (host-native) — it installs the daemon, runs setup, and restores or bootstraps your brain. When it finishes, run the mcpbrain-cowork-setup skill in Cowork. Idempotent.
---

# Install mcpbrain (Part 1 of 2 — runs in Claude Code)

**Run this skill in Claude Code** (the desktop app on your Mac), not in Cowork.
Claude Code runs on your machine, so it can install the daemon directly — run the
commands below with the Bash tool. Cowork's sandbox can't write to the home
directory or register a background agent, which is why install happens here.

This skill does the **host install only**. When it's done, you'll switch to Cowork
and run **`mcpbrain-cowork-setup`** (Part 2) for the project and scheduled tasks.

All recurring brain work runs on your Claude subscription as **Cowork** Desktop
Scheduled Tasks — no Anthropic API and no background Claude CLI.

## Step 1 — Install the daemon (run here in Claude Code)

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

Registers the background agent — **launchd** on macOS, **Task Scheduler** on Windows —
starts the daemon, and opens the wizard. Tell the user to complete **Google sign-in,
identity, and timezone**, then confirm before continuing. Use the default home
(`~/Library/Application Support/mcpbrain`) — do not set a custom MCPBRAIN_HOME.

**Fleet (Centrepoint org):** leave the pre-filled `mcpbrain-fleet` / `mcpbrain-escrow`
folder IDs as-is to join the org fleet, or clear them if not part of the org.

**Backup:**
- **Fresh start:** click **Enable backup** (generates a key, escrows it to Drive,
  starts daily snapshots).
- **Recovering an existing brain (Step 3):** do **NOT** Enable backup — restore brings
  your original key and settings back; enabling here would mint a new key.

## Step 3 — Recover an existing brain (only if you've used mcpbrain before)

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
  a successful restore, **skip Step 4** — your world-model is back.

## Step 4 — Bootstrap (fresh start only)

**Skip if you restored in Step 3.** Otherwise run the **`mcpbrain-bootstrap`** skill —
a one-time interview seeding your world-model into your records repo.

## Step 5 — Open Claude at login

Tell the user: **set Claude to open at login** so the scheduled tasks fire.
- **macOS:** System Settings → General → Login Items → add Claude.
- **Windows:** Task Manager → Startup Apps → enable Claude.

Scheduled tasks run only while Claude is open and the machine is awake; a missed run is
caught up automatically on the next wake/reopen.

---

## ✅ Part 1 done → now switch to Cowork

The brain is installed and your data is in place. **Open a Cowork session and run the
`mcpbrain-cowork-setup` skill** to create the My Brain project and the four scheduled
tasks. Do NOT create the scheduled tasks here in Claude Code — Claude Code's `/schedule`
makes a **cloud routine** that can't reach your local brain. The tasks must be **Cowork
Desktop Scheduled Tasks** (local), which is what `mcpbrain-cowork-setup` sets up.

## Idempotency

Safe to re-run: `uv tool install` is a no-op at the same version, agent registration is
idempotent, the wizard skips filled fields, and restore refuses to clobber a non-empty
store without `--force`.
