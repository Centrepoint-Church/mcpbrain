---
description: Hourly meeting-prep packs for upcoming calendar events; change-detecting, so a pack rebuilds only when its context actually changed. Uses the mcpbrain MCP tools.
---

# Meeting Packs Cowork Session

**Schedule:** Hourly (Cowork Desktop Scheduled Task).
**Working folder / project:** your `My Brain` project (the task's working folder binds it).

**Purpose:** Keep a useful prep pack ready for each upcoming meeting that warrants
one. This runs every hour and is **change-detecting**: it rebuilds a pack only
when the meeting's underlying context has actually changed since the last pack
was written, so an hourly cadence costs almost nothing on a quiet day.

Use the **mcpbrain MCP tools** throughout — `brain_meetings_today`,
`brain_meeting_pack_get`, `brain_meeting_pack_upsert`, and `brain_search`. Do NOT
use `curl`, the control API, or shell file access: the MCP server runs natively
on the host, whereas Cowork may run shell/code in an isolated VM that cannot
reach the host daemon. If a tool call errors because the daemon is down, stop and
do nothing.

## Step 1 — List today's meetings

Call **`brain_meetings_today`**. It returns today's events, each:
`{ "id", "title", "start" (HH:MM), "end" (HH:MM), "all_day" (bool), "location",
   "attendees" (list of names), "has_pack" (bool) }`.

## Step 2 — Pick events that warrant a pack

For each event, **skip** it if ANY of these are true:
- `all_day` is true
- `attendees` is empty
- duration (`end` − `start`) is under 20 minutes
- the event has already started or ended (prep packs are for *upcoming* meetings)

**Cap: touch at most 5 packs per run.** Process the soonest upcoming meetings first.

## Step 3 — Build the context signature

Compute a **context signature** for the event — a single string that changes if
and only if the inputs a pack is built from change. Build it by joining these
parts with `|`, in this exact order:

1. the event `title` (trimmed),
2. `start` and `end`,
3. the `attendees` list **sorted alphabetically**, joined by `,`,
4. the `location`,
5. the top `brain_search` hit doc-ids for the title + attendees (Step 5) — the
   ordered list of returned `doc_id`s above the 0.35 score floor, joined by `,`.

Example: `College planning|09:00|10:00|Joel Chelliah,Sam Admin|Hall B|doc-12,doc-7`.

This string **is** the `context_hash`. It is compared by exact equality — you do
not need to hash it.

## Step 4 — Decide: rebuild, or skip as unchanged

Call **`brain_meeting_pack_get`** with the event `id`. If it returns
`{"found": false}`, there is no pack yet — rebuild. Otherwise compare the stored
`context_hash` to the signature from Step 3:

- **Equal** → context unchanged: **skip this event**, do not rewrite it. Log
  `unchanged: <title>`.
- **Different (or no pack)** → **rebuild** (Steps 5–6). A differing signature
  means the title, time, attendees, location, or the most relevant background
  changed. Because the signature includes the `brain_search` doc-ids, a pack
  refreshes when *new relevant email/decisions/notes* land — not just when the
  calendar entry is edited.

## Step 5 — Gather context (only when rebuilding)

Use **`brain_search`** with the meeting title and each attendee name to find
relevant prior emails, decisions, and notes. Treat a top score < 0.35 as no real
match. (These are the same results whose ordered `doc_id`s fed the signature.)

## Step 6 — Write the pack

Build `pack_text` as markdown in this format:

```
## [Event Title] — [Date]

**Attendees:** [names]
**Purpose:** [1 sentence]

### Context
[2-3 sentences from brain_search results]

### Prep notes
- [specific items to be aware of]

### Key questions
- [if any]
```

Then call **`brain_meeting_pack_upsert`** — always pass `context_hash` (the
signature from Step 3) so the next hourly run can detect "unchanged":

- `event_id`: the event `id`
- `event_title`: the title
- `event_date`: `YYYY-MM-DD`
- `pack_text`: the markdown above
- `attendees`: the attendee names list
- `context_hash`: the Step 3 signature string

A successful response is `{"ok": true}`.

## Quality check

Each pack must have at least a **Context** and a **Prep notes** section. If a pack
is shorter than 100 words, still save it but log a warning line to stdout.

## Catch-up note

Cowork only runs a scheduled task while your computer is awake and the Claude
Desktop app is open. If a run is missed (asleep / app closed), Cowork runs it
automatically once you wake the machine or reopen the app — so a skipped hour is
caught up, not lost. The change-detection above makes those catch-up runs cheap:
packs whose context did not change are skipped.
