# mcpbrain-reference-gardener

Weekly scheduled task. Reviews what changed in the brain this week and develops the user's reference and context world-model.

Operates in two modes:

- **auto-apply** (`gardener_auto_apply: true` in config): applies changes directly in two lanes — drift (reference files) and constitution (context/identity.md, context/preferences.md) — and writes a changelog.
- **propose-only** (default): writes a proposals file to `reference/_proposals/` for human review.

## Setup

```bash
home=$(mcpbrain home)
records="$home/records"
today=$(date +%Y-%m-%d)
proposals_file="$records/reference/_proposals/$today.md"
auto_apply=$(python3 -c "
from mcpbrain import config
print('true' if config.gardener_auto_apply_enabled('$home') else 'false')
" 2>/dev/null || echo false)
```

## Step 1 — Gather evidence

Use the brain MCP tools to gather evidence from **the past 7 days only**. Limit all searches to recent activity — do not pull the full corpus history.

- `brain_search("decisions this week")` — decisions logged recently
- `brain_search("new project started")` — projects that appeared in recent threads
- `brain_search("new contact introduction")` — new contacts or org changes
- `brain_context` — read the current reference and context files to understand the world-model
- `brain_graph` — check for new orgs, people, or relations added recently that do not appear in the current reference files
- `brain_actions` — standing actions that suggest a project or org entry is missing

Read the current corpus files from `$records/`:
- `reference/projects.md`
- `reference/systems.md`
- `reference/org-context.md`
- `context/identity.md`
- `context/preferences.md`
- `context/voice.md`

## Step 2 — Compare and identify changes

For each corpus file, compare evidence against the current content. Identify:

- **New entries** — a project, person, org, or system that appears repeatedly in evidence but is absent
- **Stale entries** — an entry whose status or facts appear to have changed
- **Missing context** — a recurring topic that would benefit from a standing note

**Skip rule:** if evidence only confirms what is already recorded without contradiction — the entry exists and the facts match — do not propose a change for that entry.

Organise your findings into two lanes:

**Drift lane** (factual updates to reference files):
- `reference/projects.md` — new or updated project entries
- `reference/systems.md` — new or updated system entries
- `reference/org-context.md` — new or updated org, governance, or people entries (also develop org sections from `brain_graph` org affiliations and governance decisions)

**Constitution lane** (updates to identity and preferences):
- `context/identity.md` — additions to responsibilities, expertise, or confirmed org roles
- `context/preferences.md` — new observed preferences or working-style patterns inferred from recent feedback and decisions

**Role-attribution hard rule (constitution lane — non-negotiable):**
Only attribute a role or title to a person from:
- Their own explicit statement in a message (e.g. "I'm the operations lead for X")
- A signature block listing their title
- Owner confirmation

Never attribute a role from text you wrote, inferred from context, or derived from org structure. A missing role is better than a wrong one. If in doubt, leave the role blank.

If nothing has changed that warrants an update, stop here and do not write a proposals file. Log a `brain_note`: "Reference-gardener ran — no changes to propose."

## Step 3 — Apply or propose

### If `auto_apply = true`

**Drift lane** — for each reference file with changes, apply and commit:

```bash
cd "$records"
# edit the file content, then:
git add "reference/<filename>"
git commit -m "gardener: apply drift (reference/<filename>)"
```

Cap: no more than 20 lines changed in any single reference file per run. If a change would exceed this, add only the highest-confidence entries and leave the rest for the next run.

**Constitution lane** — for each context file with changes:

```bash
cd "$records"
# edit the file content, then:
git add "context/<filename>"
git commit -m "gardener: update identity/preferences"
```

**Write a changelog** (what was applied, not what to approve):

```bash
mkdir -p "$records/reference/_proposals"
```

Write `$proposals_file`:

```markdown
# Reference changes applied — <today>

Applied by: mcpbrain-reference-gardener (auto-apply mode)
To revert any change: `git -C <records-path> revert <commit-hash>`

## reference/projects.md

### Added
- **Project Name** — <description> (source: thread from <date>)

### Updated
- **Project Name** — changed "X" to "Y" (source: thread from <date>)

## context/identity.md

(same pattern — only include files where changes were applied)
```

Commit the changelog:

```bash
cd "$records"
git add "reference/_proposals/$today.md"
git commit -m "reference-gardener: changelog $today"
```

### If `auto_apply = false`

Write proposals to `$proposals_file`:

```markdown
# Reference update proposals — <today>

Reviewed by: mcpbrain-reference-gardener
Source evidence: brain_search, brain_graph, brain_actions

## reference/projects.md

### Add
- **Project Name** — <1-sentence description, status, team>

### Update
- **Project Name** — current says "X"; evidence suggests "Y" (source: thread from <date>)

(same pattern for other files — omit sections with no changes)

---
To apply: review each proposal, then in a Claude session say "apply the proposals from reference/_proposals/<today>.md".
```

Commit:

```bash
mkdir -p "$records/reference/_proposals"
cd "$records"
git add "reference/_proposals/$today.md"
git commit -m "reference-gardener: proposals $today"
```

## Step 4 — Surface

Call `brain_note` with a summary:

- auto-apply mode: `"Reference-gardener: applied N changes ($today) — see reference/_proposals/$today.md for changelog."`
- propose-only mode: `"Reference-gardener: N proposals ready at reference/_proposals/$today.md — review and apply any that look right."`
