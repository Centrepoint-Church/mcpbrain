---
name: mcpbrain-bootstrap
description: One-time setup interview that seeds the user's reference and context corpus for the brain.
---

# mcpbrain-bootstrap

Run this skill once, right after installing mcpbrain. It interviews you and writes your initial world-model into the records repo so the brain understands who you are, what you work on, and how you communicate.

## Resolve paths

```bash
home=$(mcpbrain home)
records="$home/records"
```

The records repo lives at `$records`. Create it if it does not exist (`git init $records`).

## Interview

Ask the user the following questions one section at a time. Wait for their answer before moving to the next section.

### Section 1 — Orgs and structure

- What organisations do you work across? For each, give a short name (e.g. "Centrepoint"), its type (church / company / school / other), and 1–2 sentences on what it does and your relationship to it.
- Who are the key people you interact with regularly? For each, give their name, title, and org.

### Section 2 — Projects and initiatives

- What are the active projects or initiatives you are working on right now? For each, describe it in 1–2 sentences, its current status, and who else is involved.

### Section 3 — Systems and tools

- What are the key systems and tools you use daily? Include software, platforms, physical venues, or anything the brain should know about.

### Section 4 — Writing voice

- How would you describe your preferred email tone? (Formal / conversational / warm / direct / etc.)
- Are there phrases you always avoid? Any words or openers that feel wrong?
- Do you have a signature style element — e.g. short paragraphs, no jargon, always name the action clearly?

### Section 5 — Working preferences

- What time zone are you in, and when do you typically work?
- What meeting types do you attend regularly, and which ones do you run?
- Any standing preferences for how you want to be reminded of things?

## Write the corpus

After all five sections, write the answers into the records repo:

**`$records/reference/projects.md`** — Section 2 answers: each project as a `## Project Name` heading with description, status, and team.

**`$records/reference/systems.md`** — Section 3 answers: each system/tool as a `## Name` heading with what it does and how it's used.

**`$records/reference/org-context.md`** — Section 1 (orgs) answers: each org as a `## Org Name` heading with type, description, your relationship to it, and key contacts.

**`$records/context/preferences.md`** — Section 5 answers: written as prose or bullet list, whatever fits the answers.

**`$records/context/voice.md`** — Section 4 answers: written as a concise voice guide the brain can use when drafting on your behalf.

### Idempotency

Before writing each file, check if it already exists and has non-template content (more than 5 non-empty lines). If so, skip it and note "already seeded" — do not overwrite.

Create parent directories if they do not exist.

### Commit

After writing all files, ensure the records repo exists and commit:

```bash
# Initialise if this is a fresh install
if [ ! -d "$records/.git" ]; then
  git init "$records"
fi
cd "$records"
git add reference/ context/
git commit -m "bootstrap: seed initial reference/context corpus"
```

## Confirm

Tell the user:

> "Your brain now has your world-model. The reference-gardener skill will keep it current each week — proposing updates based on what it observes in your email graph."

If any section was skipped (already seeded), list the skipped files.
