---
name: mcpbrain-reference-gardener
description: Weekly review of brain evidence to propose updates to the reference and context corpus (never overwrites directly).
---

# mcpbrain-reference-gardener

Weekly scheduled task. Reviews what has changed in the brain this week and proposes updates to the user's reference and context world-model. It must NOT overwrite `reference/` or `context/` files directly — all proposals go to `reference/_proposals/` for human review.

## Paths

```bash
home=$(mcpbrain home)
records="$home/records"
today=$(date +%Y-%m-%d)
proposals_file="$records/reference/_proposals/$today.md"
```

## Step 1 — Gather evidence

Use the brain MCP tools to gather recent evidence:

- `brain_search("recent decisions")` — decisions logged this week
- `brain_search("new projects")` — projects that appeared in recent threads
- `brain_search("people introductions")` — new contacts or org changes
- `brain_context` — read the current `reference/org-context.md`, `reference/projects.md`, `reference/systems.md`, `context/preferences.md`, `context/voice.md` to understand the current world-model
- `brain_graph` — check for new orgs, people, or relations that do not appear in the current reference files
- `brain_actions` — any standing actions that suggest a project or org entry is missing

Read the current corpus files from `$records/`:
- `reference/projects.md`
- `reference/systems.md`
- `reference/org-context.md`
- `context/preferences.md`
- `context/voice.md`

## Step 2 — Compare and propose

For each corpus file, compare the evidence gathered in Step 1 against the current content. Identify:

- **New entries** — a project, person, org, or system that appears repeatedly in the evidence but is absent from the file
- **Stale entries** — an entry whose status or facts appear to have changed (e.g. a project described as "planning" that recent threads show as "completed")
- **Missing context** — a recurring topic that would benefit from a standing note

If nothing has changed that warrants a proposal, stop here and do not write a proposals file. Log a `brain_note`: "Reference-gardener ran — no changes to propose."

## Step 3 — Write the proposals file

If there are proposals, write `$proposals_file` with this structure:

```markdown
# Reference update proposals — <today>

Reviewed by: mcpbrain-reference-gardener
Source evidence: brain_search, brain_graph, brain_actions

## reference/projects.md

### Add
- **Project Name** — <1-sentence description, status, team>

### Update
- **Project Name** — current says "X"; evidence suggests "Y" (source: thread from <date>)

## reference/systems.md

(same pattern — Add / Update sections; omit if no changes)

## reference/org-context.md

(same pattern)

## context/preferences.md

(same pattern)

## context/voice.md

(same pattern)

---
To apply: review each proposal, then in a Claude session say "apply the proposals from reference/_proposals/<today>.md".
```

Only include files where there are actual proposals. Omit sections with no changes.

## Step 4 — Commit and surface

```bash
mkdir -p "$records/reference/_proposals"
cd "$records"
git add "reference/_proposals/$today.md"
git commit -m "reference-gardener: proposals $today"
```

Then call:
```
brain_note("Reference-gardener: proposals for $today are ready at reference/_proposals/$today.md — review and apply any that look right.")
```

## Constraint

This skill must NOT directly edit any file under `$records/reference/` or `$records/context/` (except under `_proposals/`). Writing proposals for human review is the only permitted output. The owner approves and applies changes in a normal Claude session.
