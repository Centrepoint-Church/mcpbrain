---
name: mcpbrain-cowork-setup
description: Set up mcpbrain in Cowork after installing — creates the My Brain project and the four Cowork Desktop Scheduled Tasks, then reloads plugins. Run this in a Cowork session once the install (Part 1, via the INSTALL.md prompt) has finished in Claude Code.
---

# mcpbrain Cowork setup (Part 2 of 2 — runs in Cowork)

**Run this skill in a Cowork session**, after Part 1 (the INSTALL.md prompt) has finished
in Claude Code (daemon installed, wizard done, brain restored or bootstrapped). This sets up the
two things that live in the Cowork desktop app: the **My Brain project** and the four
recurring **scheduled tasks**.

> ⚠️ **These must be Cowork Desktop Scheduled Tasks (local), NOT Claude Code routines.**
> Claude Code's `/schedule` creates a **cloud routine** that runs on Anthropic's servers
> with no access to your local mcpbrain MCP server or files — enrichment would silently
> do nothing. Create the tasks here in **Cowork**, where `/schedule` makes a local
> Desktop Scheduled Task that can reach the brain. If you already made a Claude Code
> routine for any of these, delete it.

## Step 1 — Get the brain home path

Run this (the daemon is already installed, so the path exists):

```bash
mcpbrain home
```

It prints an absolute path, e.g. `/Users/you/Library/Application Support/mcpbrain`. You'll
paste it as the working folder below.

## Step 2 — Create the "My Brain" Cowork project

In Cowork, create a project:
- **Project name:** `My Brain`
- **Working folder:** the exact path from `mcpbrain home`. This binds the project — the
  scheduled tasks in Step 3 point at the same path, so they run inside it.
- **Project instructions** (paste verbatim):

> You are working inside my personal brain. Use the mcpbrain tools (`brain_search`, `brain_actions`, `brain_context`, `brain_read`, `brain_note`, `brain_decision`) to ground every answer in what the brain already knows before responding. When I tell you something worth remembering, write it back with `brain_note` or `brain_memory_write`. Treat the working folder as my records repo — read CLAUDE.md and the records there for context.

## Step 3 — Create four Cowork Desktop Scheduled Tasks

In Cowork, type **`/schedule`** (or Settings → Scheduled Tasks → New). For each task set
the **working folder** to the `mcpbrain home` path. Cowork's schedule options are
**hourly, daily, weekly, on weekdays, or manually** — use those:

| Task name | Schedule | Skill it runs |
|---|---|---|
| `mcpbrain-enrich` | Hourly | `mcpbrain-enrich` |
| `mcpbrain-meeting-packs` | Hourly | `mcpbrain-meeting-packs` |
| `mcpbrain-gardener` | Weekly | `mcpbrain-gardener` |
| `mcpbrain-reference-gardener` | Weekly | `mcpbrain-reference-gardener` |

`mcpbrain-meeting-packs` runs **hourly** but is change-detecting — it rebuilds a pack
only when that meeting's context changed, so hourly is cheap. All four are
subscription-only (your Claude session) — no API key, no background CLI.

**Catch-up:** a Cowork scheduled task only fires while Claude is open and the machine is
awake; a missed run is caught up automatically on the next wake/reopen.

## Step 4 — Reload plugins

```
/reload-plugins
```

Connects the mcpbrain MCP server so `brain_search`, `brain_actions`, and the other tools
are available in Cowork.

## Done

The brain is fully set up. The hourly enrich task starts building your email graph the
next time Claude is open. Run `brain_actions` in a few hours to see what it's learned.
