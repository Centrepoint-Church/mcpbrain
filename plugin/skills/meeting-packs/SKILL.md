---
name: mcpbrain-meeting-packs
description: Build and update meeting preparation packs for today's calendar events.
---

# Meeting Packs Cowork Session

**Schedule:** 07:45 and 12:00 daily
**Working folder:** app data folder (`$(mcpbrain home)`)
**Connected folder:** records repo

**Purpose:** Build and update meeting preparation packs for today's calendar events that need them.

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

## Step 2 — Pick events that need a pack

For each event in `calendar`, **skip** it if ANY of these are true:
- `all_day` is true
- `attendees` is empty
- duration (`end` − `start`) is under 20 minutes
- `has_pack` is already true AND the existing pack is fresh (see Step 3)

**Cap: build at most 5 packs per run.** Process highest-value meetings first.

## Step 3 — Check the existing pack (only if has_pack)

```bash
curl -s -H "$AUTH" "$BASE/api/meeting-packs/<event_id>"
```

Returns the pack with a `built_at` ISO timestamp (or HTTP 404 if none). Rebuild only
if `built_at` is more than 8 hours old; otherwise skip the event.

## Step 4 — Gather context

Use `brain_search` with the meeting title and each attendee name to find relevant
prior emails, decisions, and notes. Treat a top score < 0.35 as no real match.

## Step 5 — Write the pack

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

POST it (note the EXACT field names — wrong names store an empty pack silently):

```bash
curl -s -X POST -H "$AUTH" -H "Content-Type: application/json" "$BASE/api/meeting-packs/upsert" \
  -d '{
    "event_id":      "<event id from Step 1>",
    "event_title":   "<event title>",
    "event_date":    "<YYYY-MM-DD>",
    "pack_text":     "<the markdown pack above>",
    "attendees":     ["Name One","Name Two"],
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
`chore(packs): build meeting packs YYYY-MM-DD HH:MM`
(Packs live in the daemon's database, not the repo, so usually there is nothing to commit.)
