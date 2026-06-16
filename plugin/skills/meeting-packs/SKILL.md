---
name: mcpbrain-meeting-packs
description: Keep meeting preparation packs current for upcoming calendar events, rebuilding a pack only when its context has changed.
---

# Meeting Packs Cowork Session

**Schedule:** Hourly (Cowork Desktop Scheduled Task).
**Working folder / project:** your mcpbrain project (working folder = `$(mcpbrain home)`).
**Connected folder:** records repo.

**Purpose:** Keep a useful prep pack ready for each upcoming meeting that warrants
one. This runs every hour and is **change-detecting**: it rebuilds a pack only
when the meeting's underlying context has actually changed since the last pack
was written, so an hourly cadence costs almost nothing on a quiet day.

You have two capabilities: the `brain_search` MCP tool (for context) and the `Bash` tool
(for talking to the local control API over `curl`). There is no MCP tool for packs — you
MUST use `curl` against the control API to read events and read/write packs.

## Setup (run first)

Resolve the working directories and control API credentials:

```bash
home=$(mcpbrain home)
port=$(cat "$home/control_port" 2>/dev/null)
token=$(cat "$home/control_token" 2>/dev/null)
BASE="http://127.0.0.1:$port"
AUTH="Authorization: Bearer $token"
```

If `$port` is empty, the daemon is not running — stop and do nothing.

## Step 1 — List today's events

```bash
curl -s -H "$AUTH" "$BASE/api/dashboard/today"
```

The `calendar` array holds today's events. Each event object:
`{ "id", "title", "start" (HH:MM), "end" (HH:MM), "all_day" (bool), "location",
   "attendees" (list of names), "has_pack" (bool) }`.

## Step 2 — Pick events that warrant a pack

For each event in `calendar`, **skip** it if ANY of these are true:
- `all_day` is true
- `attendees` is empty
- duration (`end` − `start`) is under 20 minutes
- the event has already started or ended (prep packs are for *upcoming* meetings)

**Cap: touch at most 5 packs per run.** Process the soonest upcoming meetings first.

## Step 3 — Compute the context fingerprint

Before doing any expensive work, compute a **context fingerprint** for the event —
a short string that changes if and only if the inputs a pack is built from change.
Build it from, in this exact order:

1. the event `title` (trimmed),
2. the event `start` and `end`,
3. the `attendees` list **sorted alphabetically**, joined by `,`,
4. the `location`,
5. the top `brain_search` hit doc-ids for the title + attendees (see Step 5) —
   take the ordered list of returned `doc_id`s (above the 0.35 score floor),
   joined by `,`.

Concatenate those parts with `|` separators and hash them:

```bash
fingerprint=$(printf '%s' "$signature_string" | shasum -a 256 | cut -c1-16)
```

This `fingerprint` is the pack's `context_hash`.

## Step 4 — Decide: rebuild, or skip as unchanged

Read the existing pack (404 means none exists yet):

```bash
curl -s -H "$AUTH" "$BASE/api/meeting-packs/<event_id>"
```

- If a pack exists AND its `context_hash` **equals** the fingerprint from Step 3,
  the context is unchanged — **skip this event** (do not rewrite it). Log
  `unchanged: <title>`.
- Otherwise (no pack yet, or the fingerprint differs) — **rebuild** the pack
  (Steps 5–6). A differing fingerprint means the title, time, attendees,
  location, or the most relevant background changed.

Because the fingerprint includes the `brain_search` doc-ids, a pack is refreshed
when *new relevant email/decisions/notes* land for that meeting — not just when
the calendar entry is edited.

## Step 5 — Gather context (only when rebuilding)

Use `brain_search` with the meeting title and each attendee name to find relevant
prior emails, decisions, and notes. Treat a top score < 0.35 as no real match.
(These are the same results whose ordered `doc_id`s fed the fingerprint in Step 3.)

## Step 6 — Write the pack

`pack_text` is markdown in this format:

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

POST it (note the EXACT field names — wrong names store an empty pack silently).
**Always include `context_hash`** so the next hourly run can detect "unchanged":

```bash
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" "$BASE/api/meeting-packs/upsert" \
  -d '{
    "event_id":      "<event id from Step 1>",
    "event_title":   "<event title>",
    "event_date":    "<YYYY-MM-DD>",
    "pack_text":     "<the markdown pack above>",
    "attendees":     ["Name One","Name Two"],
    "context_hash":  "<fingerprint from Step 3>",
    "cowork_session":"meeting-packs"
  }'
```

A successful response is `{"ok": true}`. After posting, GET the pack back and confirm
`pack_text` is non-empty — if it's empty you used the wrong field names.

## Quality check

Each pack must have at least a **Context** and a **Prep notes** section. If a pack is
shorter than 100 words, still save it but log a warning line to stdout.

## Commit

If the records repo has uncommitted changes, commit with:
`chore(packs): refresh meeting packs YYYY-MM-DD HH:MM`
(Packs live in the daemon's database, not the repo, so usually there is nothing to commit.)

## Catch-up note

Cowork only runs a scheduled task while your computer is awake and the Claude
Desktop app is open. If a run is missed (asleep / app closed), Cowork runs it
automatically once you wake the machine or reopen the app — so a skipped hour is
caught up, not lost. The change-detection above makes those catch-up runs cheap:
packs whose context did not change are skipped.
