---
description: Weekly memory hygiene for the records repo — dedupe, expire stale entries, fix drift. Resolves paths via mcpbrain home; writes structured changes through the MCP tools.
---

# Memory Gardener — Weekly Hygiene Task

**Schedule:** weekly, Monday 08:00

## Setup (run first)

Resolve the working directories:

```bash
home=$(mcpbrain home)
records="$home/records"
```

`$records` is the records repo — the working directory for all git operations.
The app data folder is `$home`.

---

**Working folder:** the records repo (`$records`, i.e. `$(mcpbrain home)/records`)
**Connected folder:** the app data folder (`$(mcpbrain home)`)

## Purpose

This is a HYGIENE pass, not a primary writer. Structured writes flow through the MCP tools (`brain_decision`, `brain_note`, `brain_memory_write`, `brain_ingest`) into the daemon, which owns the records file write + commit. The gardener does NOT originate decisions, continuity notes, or memory files from scratch and does NOT hand-edit `state/decisions.md`, `state/hot.md`, or create new `memory/*.md` from raw captures — those are the daemon's job.

Each week the gardener's job is to tidy what the daemon and sessions have already written:

1. **Dedupe** — collapse memory files / MEMORY.md pointers that say the same thing; merge near-duplicates into the canonical one.
2. **Expire stale** — remove or mark memories that are no longer true (superseded behaviour, retired systems, migrated infrastructure) and drop their MEMORY.md pointers.
3. **Promote captured-but-not-promoted** — when a durable fact has been captured (e.g. landed in `context/memory.md` / the note index) but never surfaced as a proper `memory/*.md` pointer, add the pointer so it's discoverable. This is reorganising existing captured content, not authoring new facts.
4. **Fix drift** — correct MEMORY.md descriptions/pointers that no longer match the files they point at; remove pointers for files that no longer exist.

If nothing needs tidying, log that instead — don't make cosmetic edits. If you find a durable fact that has NOT been captured anywhere, do not write it yourself; flag it for capture through the MCP tools rather than writing it directly.

## What to read first

1. `$(mcpbrain home)/context/memory.md` — current memory index (daemon-maintained)
2. `$records/state/hot.md` — entries from the last 7 days
3. `$records/MEMORY.md` — the Claude Code auto-memory index

`$(mcpbrain home)/context/memory.md` is the daemon-maintained note index; the records repo `MEMORY.md` is the Claude Code auto-memory index maintained by the gardener. They are different files with different owners.

## What you can update (hygiene only)

- `MEMORY.md` — dedupe pointers, fix drifted descriptions, remove pointers for files that no longer exist, add a pointer for an already-captured memory file that lacks one
- `memory/*.md` files — dedupe (merge near-duplicates into the canonical file), expire stale ones, fix factual drift in an existing file. Do NOT author a brand-new memory file from raw captures — that goes through `brain_memory_write` → daemon.
- `reference/` files — tidy/dedupe project and context that's already captured; correct drift

## What you cannot modify

- Anything between `<!-- GARDENER-PROTECTED-START -->` and `<!-- GARDENER-PROTECTED-END -->` in `CLAUDE.md`
- `context/identity.md`, `context/voice.md`, `context/preferences.md`
- `state/decisions.md`, `state/hot.md`, `state/retired.md`, `state/compliance.md` (daemon-owned via the MCP write tools — never hand-edit)
- `CLAUDE.md` itself (outside the non-protected routing sections — those sections require manual review before changes)

## What you must NOT do (primary writing belongs to the MCP tools + daemon)

- Do not author new decisions, continuity notes, or memory files from scratch. Those are written via `brain_decision` / `brain_note` / `brain_memory_write` → daemon → records file + commit.
- Do not hand-edit `state/decisions.md` or `state/hot.md` to add content.
- If you spot an uncaptured durable fact, flag it for capture through the MCP tools rather than writing it directly.

## Caps per run

- Max 10 memory file updates (create or modify)
- Max 20 lines changed in any single reference file
- No changes to protected sections under any circumstances

## Commit format

Stage specific files by name (never `git add -A`):

```bash
git add state/decisions.md reference/projects.md memory/feedback_xyz.md MEMORY.md
git commit -m "gardener: [brief description of what changed and why]"
```

Example messages:
- `gardener: merge duplicate project status entries; update projects.md`
- `gardener: expire stale memory about retired infrastructure; no other changes`

## If nothing needs changing

```bash
echo "$(date -I): gardener ran, no changes needed" >> "$(mcpbrain home)/gardener.log"
```

## Quality check before committing

Read every file you changed. Confirm:
- No banned words (crucial, pivotal, leverage, robust, streamline — see context/voice.md)
- No em dashes
- Org tags are internally consistent
- Role attributions only from confirmed sources
