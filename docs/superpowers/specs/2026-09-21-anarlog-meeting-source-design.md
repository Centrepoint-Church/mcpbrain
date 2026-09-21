# anarlog as a meeting source: transcripts and AI notes into the brain

Date: 2026-09-21
Status: design approved, not implemented

## Problem

Meetings are the largest category of the user's working life that the brain
cannot see. Gmail, Drive and Calendar are ingested; what was actually *said and
decided* in a meeting is not. Calendar gives the brain a meeting's existence,
time and attendees, and nothing about its content.

Granola was the incumbent capture tool and is **not viable as a source**, for two
independent reasons established by investigation on 2026-09-17:

1. **Its local data is encrypted.** Granola 7.568.0 stores `cache-v6.json.enc`
   plus a `granola.db` whose header is random bytes (`sqlite3` reports "file is
   not a database"). Every community exporter targets the old plaintext
   `cache-v3.json`, which no longer exists. Local scraping is dead and would
   break again on any Granola release.
2. **Its API is plan-gated.** `public-api.granola.ai` requires a Business plan
   (USD $14/user/month). OAuth to Granola's hosted MCP succeeds on any plan
   because OAuth proves *identity, not entitlement* — which is why anarlog's UI
   reported "Granola is connected" while importing nothing, with no error
   logged. An empty list is not a failure.

anarlog (formerly Hyprnote, `fastrepl/anarlog`, MIT) records meetings locally and
stores sessions, notes, summaries and transcripts in a local SQLite database.
Its free tier includes unlimited on-device transcription and a local API/CLI/MCP.
It is already installed, already capturing, and already holds real content: a
one-time Granola CSV import brought in two meetings carrying 7.9k/17.3k chars of
notes and summaries and 50,780/124,322 chars of transcript.

## Goals

- Ingest anarlog meetings — note, summary and transcript — into the brain as
  chunks, on the daemon's normal sync cycle.
- Incremental: only changed sessions each cycle, via a durable watermark.
- Deletions in anarlog propagate to the brain.
- Meeting content links to the existing calendar chunks (`cal-<event_id>`),
  `meeting_packs`, and meeting-series entities.
- Enrichment cost stays bounded: transcripts are searchable but never enriched.
- Meeting-derived claims never reach the org shared graph.
- An anarlog schema change degrades **loudly**, never silently.

## Non-goals

- Writing to anarlog. The read path is strictly read-only; anarlog's own docs
  warn that editing its database corrupts notes, and this repo has a documented
  store-corruption incident (2026-09-10) caused by two concurrent writers. We
  will never be anarlog's second writer.
- Speaker attribution. Diarization is a paid anarlog feature and the imported
  transcripts carry an empty `speaker_hints_json`. Transcripts are unattributed
  text; the design does not pretend otherwise.
- A separate backfill phase. The corpus is small; the first cycle's watermark
  walk from an empty cursor *is* the backfill.
- Migrating Granola history beyond what the user imports by CSV themselves.

## Established facts (verified, not assumed)

Verified against the live anarlog database on 2026-09-17/21:

| Fact | Evidence |
|---|---|
| CLI has **no** `updated_after` filter | `meetings list` accepts only `--query`, `--series-id`, `--limit` (max 200), `--offset` |
| `sessions.updated_at` is a usable watermark | distinct from `created_at` on real rows |
| Soft-delete bumps the watermark | all 15 deleted sessions satisfy `updated_at >= deleted_at` |
| Schema version signal exists | `_sqlx_migrations`, timestamped (`20260909160300`) |
| `_anlg_schema_compat` is useless for drift | single row, `min_supported_version = 0` |
| Note/summary bodies | `body_format = 'prosemirror_json'`, `{"type":"doc","content":[...]}` |
| Transcript body | `transcripts.words_json`, ordered array of `{id, text, ...}` |
| Calendar linkage exists | `sessions.event_id`, `external_event_id`, `series_id` |
| Cold chunks already blocked from org contribution | `org_contrib.collect_from_drain` — "no/cold provenance — fail closed" |

The CLI was evaluated and rejected as the read path: with no watermark it forces
a full re-scan every cycle. Direct read-only SQLite was chosen deliberately,
accepting schema-drift risk in exchange for a real watermark, deletion
detection, and the `event_id`/`series_id` fields the CLI does not expose.

## Design

### 1. `mcpbrain/sync/anarlog.py`, a normal sync source

Mirrors `sync/calendar.py`:

```python
discover_anarlog(store, *, db_path, budget=None, bulk_section=None) -> int
handle_anarlog_item(store, item, *, db_path, bulk_section=None) -> None
```

`discover_anarlog` enqueues changed session ids into `sync_queue` with
`source="anarlog"`. `handle_anarlog_item` works one session and raises on
failure so `work_queue` backs it off — a malformed prosemirror body must not
take down the cycle.

Registered in `run_sync_cycle`'s `handlers` dict as `handlers["anarlog"]` and
discovered alongside the other sources, gated on the database existing:

```python
if anarlog_db is not None:
    discovered["anarlog"] = discover_anarlog(store, db_path=anarlog_db, budget=disc_budget)
    handlers["anarlog"] = lambda it: handle_anarlog_item(store, it, db_path=anarlog_db)
```

Unlike its siblings it takes no `service` — the "connection" is a resolved path.

**Ingestion is EXPLICIT OPT-IN**, via `anarlog.enabled` in `<home>/config.json`.
With the flag absent or false, `config.anarlog_db_path(home)` returns `None`
without reading `Path.home()` at all, and the cycle is untouched — no
`discovered` key, no handler, no log line. Once enabled, it resolves
`anarlog.db_path` if set, else the macOS default
`~/Library/Application Support/anarlog/app.db`, returning `None` if that file
does not exist.

Opt-in rather than auto-detect, for two reasons found during implementation.
**Correctness:** an accessor that reads the OS home on every call is not a
function of `home`, so merely having anarlog installed made 13 pre-existing
sync-cycle tests fail — the suite became dependent on what the dev machine had
installed. **Consent:** mcpbrain ships to other people. Meeting transcripts
contain third parties' recorded speech, and whether to ingest them is the
user's decision, not a consequence of an app being present on disk. This also
matches the repo's standing precedent that a new capability ships OFF pending
config plus real-data validation.

**`sync_queue` is retained even though the read is free.** The read costing
nothing does not make the *work* free: chunking and embedding 124k characters of
transcript is real CPU on the daemon's single process, which is exactly what
`work_queue`'s per-cycle bound exists for, and the queue supplies retry/backoff
that a cadence pass would not.

### 2. Read-only access, always

Every connection uses `file:<path>?mode=ro`. This is non-negotiable: anarlog is
the sole writer of its database, and the 2026-09-10 corruption incident in this
repo was caused by two concurrent writers to a single-writer SQLite store.

### 3. Schema-drift protection

The risk accepted by reading a private schema is handled explicitly.

`discover_anarlog` reads `SELECT MAX(version) FROM _sqlx_migrations` and compares
it against `_PINNED_SCHEMA_VERSION` (currently `20260909160300`). On a mismatch
it validates the columns actually used:

- `sessions`: `id, title, updated_at, deleted_at, started_at, event_id, series_id, external_provider`
- `session_documents`: `session_id, kind, body, body_format, deleted_at`
- `transcripts`: `session_id, words_json, deleted_at`

Every column the module names in SQL appears in this map — including
`deleted_at` on the child tables, which `read_session` filters on. A column
read but not declared would break at ingest time instead of being caught by
the guard.

If every column is present, the source proceeds and logs the new version **once**
(not per cycle). If any column is missing, the source **stops and reports the
missing columns** — it does not partially ingest.

The property that matters: an anarlog upgrade either keeps working or fails
visibly. It never silently writes wrong or partial content. `_anlg_schema_compat`
is not used; it carries no version information.

### 4. doc_id scheme and chunk metadata

Following `cal-<event_id>` and `gdrive-<file_id>-<n>`:

```
anarlog-<session_id>-summary-<i>
anarlog-<session_id>-note-<i>
anarlog-<session_id>-transcript-<i>
```

Metadata written on every chunk:

```python
{
  "source_type": "anarlog",
  "session_id": <uuid>,
  "content_subtype": "summary" | "note" | "transcript",
  "meeting_title": <title>,
  "started_at": <iso>,
  "event_id": <google event id or "">,
  "series_id": <recurrence series id or "">,
}
```

`content_subtype` is load-bearing — see §6.

### 5. Text extraction

**prosemirror → markdown.** Bodies are `{"type":"doc","content":[...]}`. A
recursive walk collects `text` nodes; `heading` nodes emit `#` prefixes by
`attrs.level`; `paragraph`, `bulletList`/`orderedList` and `listItem` emit
blank-line and `- ` structure. Structure is preserved because the summary's
headings are what make it readable in recall.

**transcript.** `words_json` is an ordered array of `{id, text, ...}`; text is
the `text` fields joined in order. This works for both imported transcripts
(coarse blocks, ids like `meeting-import:<hash>:word:0`) and live-recorded ones
(true word-level with timings) without branching.

Both results then pass through the existing `chunking.chunk_text`.

### 6. Transcripts are ingested cold

`prepare.should_enrich` gains **one source-agnostic line**, beside the existing
`content_subtype == "table"` check whose comment already argues that tabular
data is not worth extraction *whoever produced it*:

```python
if str(meta.get("content_subtype") or "").lower() in ("table", "transcript"):
    return False
```

A transcript is the same category: verbatim speech, not prose worth entity
extraction. Adding it source-agnostically means any future transcript source is
honoured without touching the gate again.

The handler performs **no cold-marking of its own**. Tagging the chunk is
sufficient: `prepare.py`'s batch classifier runs `should_enrich` over every
chunk and writes `store.set_enrich_state`. Cold chunks stay embedded and
searchable (`embedded=1`); only graph extraction skips them.

Cost, measured on the two real meetings: ~22k tokens/meeting stored and
searchable, ~2k tokens/meeting entering enrichment.

### 7. Incremental sync and deletions

Cursor key `anarlog` in `sync_cursors`, holding the highest `updated_at` seen.

```sql
SELECT id, updated_at, deleted_at
FROM sessions
WHERE updated_at >= :cursor
ORDER BY updated_at
LIMIT :n
```

`>=`, not `>`, deliberately: it re-reads the boundary row rather than risking a
skip on equal timestamps, and `upsert_chunk`'s id+hash checkpointing makes the
re-read a no-op. This is the same boundary convention `org_contrib` already
documents.

Rows with `deleted_at IS NOT NULL` enqueue `{"event": "remove"}`; others enqueue
an upsert. A single watermark covers both because soft-delete bumps
`updated_at` — verified on all 15 deleted sessions in the live database.

The cursor advances only after the discovered batch is durably enqueued,
matching `discover_calendar`'s contract: an interrupted discovery costs a
re-list next cycle, never lost work.

### 8. Deletion resolution

`handle_anarlog_item`'s remove branch mirrors `handle_calendar_item`:

```python
doc_ids = store.doc_ids_for_messages([f"anarlog-{item['ref_id']}"])
if doc_ids:
    store.delete_chunks(doc_ids)
```

`store.doc_ids_for_messages` gains a fifth case beside message_id, file_id,
thread_id and event_id: an `anarlog-<session_id>` key resolves against
`metadata.$.session_id`, returning every chunk of that session across all three
subtypes. The SQL fragment is built through `store._meta_extract()` so the index
expression and the query expression cannot drift — the failure mode that caused
the 0.7.105 full-scan outage. A matching expression index on
`metadata.$.session_id` is created in `init()`.

### 9. Calendar and meeting-series linkage

`sessions.event_id` holds the Google Calendar event id, which is exactly the key
behind `cal-<event_id>`. Carrying it in chunk metadata makes a meeting's content
and its calendar entry mutually resolvable, lets `meeting_packs` (keyed on
`event_id`) pick up real notes instead of calendar stubs, and feeds `series_id`
into the meeting-series entities added in 0.7.87.

This linkage is the reason to build a real source rather than push notes through
the capture spool.

### 10. Meetings never contribute to the org graph

The install is fleet-pinned (`fleet_secret` present,
`relation_allowlist = ['works_at', 'member_of', 'mentioned_with']`), so enriched
meeting notes would otherwise contribute relation claims to the shared org
graph. The user's meetings include ACC Staff and State Secretaries meetings —
personnel-adjacent by nature.

`org_contrib.collect_from_drain` gains one check, placed with its existing
fail-closed guards:

`_source_kind` currently maps `{"gmail": "email", "drive": "drive",
"calendar": "calendar"}` and returns `"unknown"` for anything else — so a check
against `"anarlog"` would never fire. It gains the new source first, honouring
its own "honest labelling" contract:

```python
return {"gmail": "email", "drive": "drive", "calendar": "calendar",
        "anarlog": "meeting"}.get(st, "unknown")
```

and the guard then blocks on the mapped kind:

```python
if _source_kind(store, doc_id) == "meeting":
    continue   # meetings never contribute — personnel-adjacent by nature
```

Transcripts were already excluded by the existing cold-provenance check; this
covers the hot summary and note. Note that contribution ships redacted *claims*
with a hashed `source_ref`, never text — the block is about the claims, not
leakage of transcript content.

### 11. Tests

Tests build a **real temporary SQLite file** in anarlog's shape. They do not
mock the database. This repo has been bitten repeatedly by fakes that hid real
defects: the same `Store(str(home))` constructor defect appeared three separate
times (`bin/resalience.py`, `tools._bump_unit_attempts`, `bin/enrich_ab.py`),
invisible to every test because they all used fakes that never exercised the
real constructor; and `_bump_unit_attempts` shipped inert twice, passing its
tests against fakes while covering only 22% of real units.

Coverage:

1. prosemirror extraction — headings, lists, nested marks, empty body
2. transcript assembly — ordered join, both imported and word-level shapes
3. watermark boundary — equal-timestamp row re-read is a no-op, not a skip
4. deletion propagation — soft-deleted session removes all three subtypes
5. drift detection — missing column stops the source; new version with valid
   columns proceeds and logs once
6. `should_enrich` returns False for `content_subtype="transcript"` and True for
   `"summary"`/`"note"`
7. `collect_from_drain` emits nothing for an anarlog-sourced doc_id
8. `doc_ids_for_messages` resolves an `anarlog-<session_id>` key, and the query
   plan uses the index rather than scanning (mirroring
   `test_metadata_jsonb.py`'s re-init test, which catches DDL/query drift on an
   already-initialised store)

## Follow-ups (recorded, not in scope)

- **Speaker attribution.** Unattributed transcripts limit who-said-what
  extraction. Options if this matters later: anarlog Pro ($15/mo) for automatic
  diarization, its free manual speaker labeling, or a separate local pyannote
  pass. Not required for the summary/note enrichment this design targets.
- **`Busy` calendar events.** Two of the user's calendars expose free/busy only,
  so those meetings carry no title, attendees or link. This degrades the tray
  agenda and any `event_id` linkage for those events. It is a calendar sharing
  configuration problem, not an mcpbrain one.
- **anarlog CloudSync errors.** The app logs `credential exchange unavailable;
  retrying` and multi-second `cloudsync_*` queries. Harmless on the free tier
  (CloudSync is a Pro feature) and unrelated to this source, but it is noise in
  the log if that log is ever consulted for diagnosis.
- **Writing back to anarlog.** Its CLI/MCP expose `propose_summary_edit` /
  `propose_memo_edit`, which stage a proposal for the user to apply in the
  desktop app. A future "brain proposes a correction to a meeting note" flow is
  possible but deliberately out of scope here.
