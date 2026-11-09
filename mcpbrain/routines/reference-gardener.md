# mcpbrain-reference-gardener

Weekly scheduled task. Reviews what changed in the brain this week and develops the user's reference and context world-model.

The owner picks the mode in the console (Settings > Automatic changes > reference and context files), read here with `config.auto_change_mode(home, "reference")`:

- **apply** (the default): applies changes directly in two lanes — drift (reference files) and constitution (context/identity.md, context/preferences.md) — and writes a changelog.
- **ask**: makes the same `brain_gardener_apply` calls; the tool runs every guard and queues each change for the owner to approve or decline in the console instead of writing it.
- **off**: makes no changes this run.

## Setup

```bash
home=$(mcpbrain home)
records="$home/records"
today=$(date +%Y-%m-%d)
proposals_file="$records/reference/_proposals/$today.md"
mode=$(python3 -c "
from mcpbrain import config
print(config.auto_change_mode('$home', 'reference'))
" 2>/dev/null || echo off)
```

If `mode = off`, stop here: log a `brain_note` "Reference-gardener skipped: automatic reference changes are off." and make no changes.

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

## Step 3 — Apply or queue

### Both modes (`mode = apply` or `mode = ask`)

Send every change through the **`brain_gardener_apply`** tool — never by editing the
files and running `git` yourself. The tool routes the write through the role-attribution
guard, the per-run change cap, and the correct commit tag, and returns the result so you
get immediate enforcement feedback. Pass the **full new content** of the file each call.

**Drift lane** — for each reference file with changes:

```
brain_gardener_apply(
  lane="reference",
  filename="<projects.md | systems.md | org-context.md>",
  content="<full new file content>",
)
```

The tool enforces a per-run change cap (default 20 changed lines/file). If it returns
`{"applied": false, "error": "change cap exceeded ..."}`, apply only the highest-confidence
entries this run and leave the rest for the next.

**Constitution lane** — for each context file with changes:

```
brain_gardener_apply(
  lane="context",
  filename="<identity.md | preferences.md>",
  content="<full new file content>",
  # Set these ONLY when the update assigns a role/title to a PERSON:
  asserts_person_role=true,
  attribution_source="owner_statement | signature | owner_confirmation",
  attribution_quote="<verbatim text from the source that states the role>",
  attribution_doc_id="<doc_id of the chunk that quote lives in>",  # owner_statement/signature
)
```

Leave `asserts_person_role` unset (false) for preferences, your own responsibilities, or
any update that makes no person-role claim — those need no attribution.

When you *do* assert someone's role, the claim is **verified, not trusted**: pass the
verbatim `attribution_quote` and, for `owner_statement`/`signature`, the `attribution_doc_id`
of the stored chunk it came from (use the doc_ids from your `brain_search`/`brain_graph`
evidence). The tool fetches that chunk and rejects the write if the quote is not actually in
it. `owner_confirmation` is only for a role the owner confirmed to you directly this session —
pass the confirmed wording as the quote. If you cannot cite a real source, **leave the role
out** — a missing role is better than a wrong one.

In `ask` mode a call that passes every guard returns
`{"applied": false, "queued": true, "status": "queued for review ..."}`: the change waits
in the owner's console (Needs you) and is written only if they approve it. A guard
failure (`"error"`) is reported the same way in both modes; fix and resend as above.

### If `mode = apply`: write a changelog

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

### If `mode = ask`

Write no changelog or proposals file: each queued change is already listed in the
owner's console with Apply and Decline, and approving writes and commits it exactly as
apply mode would.

## Step 4 — Surface

Call `brain_note` with a summary:

- apply mode: `"Reference-gardener: applied N changes ($today) — see reference/_proposals/$today.md for changelog."`
- ask mode: `"Reference-gardener: N changes queued for your review in the console ($today)."`
