---
session: meeting-packs
schedule: 07:45 and 12:00 daily
working_folder: app data folder
connected_folder: records repo
---

# Meeting Packs Cowork Session

**Purpose:** Build and update meeting preparation packs for today's calendar events that need them.

You have two capabilities: the `brain_search` MCP tool (for context) and the `Bash` tool
(for talking to the local control API over `curl`). There is no MCP tool for packs — you
MUST use `curl` against the control API to read events and read/write packs.

## Setup (run first)

The control API base URL, auth token, and today's date are provided in the session context
appended to this prompt. Extract them and assign to shell variables before proceeding:

```bash
BASE="<base_url from session context>"
TOKEN="<token from session context>"
AUTH="Authorization: Bearer $TOKEN"
```

If the base URL or token is missing, the daemon is not running — stop and do nothing.

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
