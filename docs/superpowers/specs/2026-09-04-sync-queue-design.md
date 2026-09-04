# Sync as a durable work queue — design

**Date:** 2026-09-04
**Status:** approved design, pre-implementation
**Scope:** replace the batch-then-commit sync round in Drive, Gmail and Calendar with
`discover → sync_queue → work`. One abstraction, all three sources.

## Problem

Sync is **batch-then-commit over an unbounded batch, cut by a wall-clock budget**:

```
page the whole feed → accumulate `pending` (unbounded) → write it all → advance cursor
                ↑ the budget can cut anywhere in here, and the commit is all-or-nothing
```

This shape has produced the same defect five times. Each was fixed at its own site;
none of them fixed the shape.

| patch | what its own comment says |
|---|---|
| `CYCLE_BUDGET_S` (60s) | bound the cycle so maintenance passes are not starved |
| `CAPTURES_BUDGET_S` (10s) | the shared budget "is already fully spent by the time drain_captures would run" |
| `ENRICH_SPOOL_BUDGET_S` (60s) | "the exact same defect `CAPTURES_BUDGET_S` was created to fix … just found later" |
| `"Minimum forward progress"` ×2 in `sync/drive.py` | a round could write zero items and re-do identical work |
| `<source>:page_token` (0.7.123) | the paging loop had no forward-progress guarantee at all |

`sync/gmail.py`'s docstring already describes the identical livelock in its own words:
a budget that covers fewer messages than the delta contains means messages are
"PERMANENTLY never ingested … genuinely worse than the pre-budget" behaviour.

### What it cost, measured

The 0.7.123 investigation found the `drive` cursor had not advanced since
**2026-07-29 — five weeks** — while `gmail` and `calendar` advanced daily. The cause:
a round advances the cursor only if it finishes clean, and Drive returns
`newStartPageToken` **only on the feed's last page**, so once the backlog outgrew one
`CYCLE_BUDGET_S` every round ended with `new_start=None` *and* `interrupted=True`.
Two independent reasons the cursor could not move. Each cycle re-walked the same
~5,000-change prefix and discarded it.

- ~25% of a core burned continuously (in ~20s bursts at ~85% every 60s)
- 434 `ingest_skip` rows/day into the user-facing `change_log`
- **no Drive change ingested for five weeks** — the freshness cost, far worse than the CPU

0.7.123 fixed the livelock. It did not fix the shape, and three consequences remain:

1. **Throughput is ~1 file/cycle** through document-dense spans, because the paging
   offset may only advance once every file in the paged span is written.
2. **`resume_ids` is a JSON blob re-sorted and re-serialised on every single file**
   (`store.set_cursor(resume_key, json.dumps(sorted(resumed_ids)))`, inside the
   per-file loop) — O(n²) writes per span.
3. **Backlog is inferred, never stored.** This is why the outage hid for five weeks:
   nothing could answer "how far behind is Drive?" and `doctor` was green throughout.

A fourth defect is documented in `sync/drive.py` and unaddressed — a transient export
failure (TLS reset, 5xx) marks the file done anyway, because the cursor will move past
it: *"this version is DROPPED and will not be retried until the file changes again."*
The comment names the real fix as "a bounded per-file attempt counter, like
`chunks.enrich_attempts`".

## Goals

1. Make the livelock class structurally impossible, in all three sources.
2. Make the backlog a stored fact, surfaced in `doctor`.
3. Close the silent data loss on transient failure.
4. Fix catch-up throughput as a consequence, not as a separate lever.

**Non-goals:** the enrichment spool, `brain_actions` latency (~8.9s), and the
68,007-item `bin/repair.py` re-chunk backlog. All separate.

## Approach

Split **discovery** (cheap, listing only) from **work** (expensive, per item), with a
durable SQLite queue between them.

| stage | cost | commits | effect of a budget cutoff |
|---|---|---|---|
| **discover** — page the delta, UPSERT rows, advance cursor | ms/page, no fetch | per page | resumes at the last committed page |
| **work** — claim N, fetch/extract/upsert, delete row | seconds–minutes/item | per item | nothing lost, nothing repeated |

### Why SQLite and not a file spool

`enrich_queue/` is files because its consumer is an **external LLM session** reaching in
through MCP tools. Sync's consumer is the daemon itself, in-process, so the constraint
is different and two properties decide it:

- **Atomicity.** The queue row and the chunk write commit in **one transaction**, so
  "these chunks exist" and "this item is done" are a single fact. That removes by
  construction the crash-between-write-and-checkpoint window that `resume_ids` exists to
  paper over.
- **Consistent restore.** Backup/restore takes the SQLite file. Queue, cursors and
  chunks come back at the same point in time. This matters more than usual because the
  cursor now advances at *discovery* time, ahead of the work — see Risks.

**The queue must never move out of the store.** That is the property the design rests on.

## Schema

```sql
CREATE TABLE IF NOT EXISTS sync_queue(
  source          TEXT NOT NULL,   -- 'drive' | 'drive:<drive_id>' | 'gmail' | 'calendar'
  ref_id          TEXT NOT NULL,   -- file_id / message_id / event_id
  version         TEXT NOT NULL DEFAULT '',
  event           TEXT NOT NULL,   -- 'upsert' | 'remove'
  modified_at     TEXT,            -- ISO; the newest-first sort key
  discovered_at   TEXT NOT NULL,
  attempts        INTEGER NOT NULL DEFAULT 0,
  next_attempt_at TEXT,            -- NULL = ready now
  last_error      TEXT NOT NULL DEFAULT '',
  PRIMARY KEY (source, ref_id)
)
```

- **No `state` column.** Presence *is* pending; success deletes the row. The backlog is
  `SELECT count(*)` — a fact, not an estimate — and the table stays small.
- **`PRIMARY KEY (source, ref_id)` gives per-item event collapse for free.**
  Re-discovering a file UPSERTs over its row, so change-then-removal collapses to
  removal exactly as `sync_shared_drive`'s in-memory `events` dict does today — but
  durably, and across cycles rather than only within one round.
- **A differing `version` on UPSERT resets `attempts` to 0.** A genuinely new edit is
  new work, not a continuation of a failing one.
- **No lease column.** The daemon is the only worker and holds the single-writer lock;
  a crash mid-item leaves the row present, so it simply retries. Leases are what
  `enrich_queue` needs because its workers are external. Adding them here is speculative.

## Discovery

Discovery pages the provider delta, UPSERTs rows, and advances the cursor. It performs
no fetch, export or extraction.

> **Invariant: each page's rows and that page's cursor advance commit in one transaction.**

The cursor therefore can never be ahead of what is recorded, and it advances **per page**
rather than per completed round. That dissolves the class:

- `newStartPageToken`-only-on-the-last-page stops mattering: `nextPageToken` is itself a
  valid resume point, so progress no longer requires reaching the end of the feed.
- The cursor returns to exactly one meaning — *the provider token to resume listing from*.
  The dual role split in 0.7.123 does not need splitting; it needs deleting.

Per-source discovery stays separate, because the mechanisms genuinely differ (Drive page
tokens, Gmail history ids, Calendar syncToken). Each shrinks to a loop that enqueues and
commits. Everything downstream is shared.

### Budgets stop being correctness-critical

Today a cutoff mid-round destroys that round's work, which is why three separate budgets
were carved out to stop phases starving each other. After this, a cutoff anywhere is
free. Budgets become pure scheduling. Discovery gets a small dedicated slice (**15s**) and work
takes the remainder of `CYCLE_BUDGET_S`, so a large queue cannot starve discovery of new
changes — a fairness choice, no longer a bug fix. `CAPTURES_BUDGET_S` and
`ENRICH_SPOOL_BUDGET_S` are out of scope and unchanged.

## Work

New module `mcpbrain/sync/queue.py` holds the store-agnostic queue logic and the work
loop; the per-source `handle()` dispatch lives there and calls into the existing
`sync/drive.py`, `sync/gmail.py` and `sync/calendar.py` fetch/extract functions unchanged.

```
work_queue(store, services, *, limit, budget):
    for item in store.due_sync_items(limit=limit, now=now):     # newest-first
        if budget.expired():
            break                      # free: the row is still queued
        try:
            handle(item)               # per-source fetch → extract → chunks
            store.complete_sync_item(item)   # SAME transaction as the chunk write
        except Exception as exc:
            store.fail_sync_item(item, exc)
```

The per-source `handle()` dispatch reuses the existing fetch/extract code unchanged.

**Ordering: `ORDER BY modified_at DESC`.** A document edited now is the next thing
worked, even with tens of thousands of items behind it, so it is searchable within a
cycle. Per-item collapse means newest-first cannot reorder an item's own history —
there is only ever one row per item, holding its latest state — so ordering is only
*across* items and change/removal sequencing stays correct.

`due_sync_items` is a pure read — there is no lease to take, so the name says so.

**`limit` replaces the budgets as the operator knob.** Start at **50 items/cycle** and
tune against the live queue; it is a config value, not a constant. "How many items per cycle" has an
obvious meaning; "how many seconds of mixed-cost work" does not, when unit costs span
from a microsecond mime-skip to a 60-second OCR PDF. The budget remains as a backstop so
one slow item cannot run the cycle long.

### What gets deleted

| removed | why |
|---|---|
| `<source>:resume_ids` (drive, shared drive, gmail) | rounds no longer exist |
| `<source>:resume_removed_ids` | same |
| `<source>:page_token` (0.7.123) | the cursor advances per page |
| both `"Minimum forward progress"` guards | a zero-item cycle is now harmless |
| `if not pagination_interrupted:` | discovery and work are separate phases |
| the O(n²) JSON re-serialise per file | a row update replaces the blob rewrite |
| `gmail.py`'s livelock-workaround docstring | no longer true |

## Failure and retry

Never give up; surface persistent failures.

```
attempts += 1
next_attempt_at = now + backoff(attempts)     # 2m → 10m → 1h → 6h → 24h (capped)
last_error      = str(exc)[:200]
```

The row stays. Nothing is ever abandoned, and a permanently broken file settles at one
retry per day — bounded cost, permanently visible. **`doctor` surfaces any item with
`attempts >= 3`**; retries continue regardless.

**No head-of-line blocking**, the property that makes never-give-up safe:
`due_sync_items` filters on `next_attempt_at`, so a failing item is not selected until its backoff
expires. It cannot sit at the head of a newest-first queue and stall everything behind
it. Without that filter, "retry forever" would be a way to wedge the pipeline on one
poison PDF.

This closes the `KNOWN GAP`, which is deleted rather than updated.

**Deliberately not special-cased:** a transient outage (expired token, network down)
fails every claimed item at once and pushes them all onto backoff, delaying recovery by
up to the backoff even after the outage ends. Detecting "is this error global?" is
guesswork, and the 2-minute first backoff makes the practical cost small. A
circuit-breaker is more machinery than the problem warrants.

## Visibility

Three `doctor` lines, because backlog invisibility is one of the three goals:

```
✅ Sync queue       0 pending
⚠️ Sync queue       37,124 pending (oldest discovered 2026-09-04 11:20)
⚠️ Sync failures    2 items retrying (Hardy Report.pdf, 7 attempts: export timeout)
```

The oldest-`discovered_at` figure exists to expose the tail-starvation risk accepted with
newest-first ordering: a backlog that is not shrinking shows up as an age that keeps
climbing, rather than as five weeks of silence.

## Migration

Unusually cheap, because the cursors already mean the right thing.

- `sync_queue` is created by `store.init()` with `CREATE TABLE IF NOT EXISTS`.
- Existing `drive`/`gmail`/`calendar` cursors are already provider resume tokens, so
  discovery picks up exactly where it is. **No backfill, no data conversion.**
- A one-shot cleanup deletes the obsolete `:resume_ids`, `:resume_removed_ids` and
  `:page_token` rows.
- `bin/optimise_store.py` needs no change — verified: it enumerates tables generically
  from `sqlite_master`, so the rebuild copies `sync_queue` automatically.

**The Drive backlog resolves as a side effect.** Discovery does no fetching, so it should
walk the remaining ~37,000 changes and enqueue them within a few cycles — today it is the
*work* that makes walking slow. Work then drains newest-first, so recent edits land
almost immediately and the older tail fills in behind.

## Risks

**The cursor advances before the work is done.** If the queue were lost while the cursor
persisted, those changes would never be re-discovered. Two things make this acceptable:

- Restore is consistent: queue, cursors and chunks are one file, one backup, one point
  in time.
- **Discovery is idempotent** (`UPSERT` on `(source, ref_id)`), so a cursor can be wound
  backwards by hand and discovery will re-enqueue. A genuine operational escape hatch
  that today's design does not have.

**Rollback is not "revert the wheel."** The old path is deleted rather than flagged —
keeping both means maintaining two sync shapes, which is the thing being removed — so a
code rollback would strand queued-but-unworked items. Recovery is restore-from-backup or
a manual cursor rewind. **Gate the live migration on a freshly verified backup**, same
posture as the store rebuild (runbook §7).

**Tail starvation** under pure newest-first, accepted deliberately; made visible by the
oldest-`discovered_at` line rather than mitigated by a reserve quota.

## Testing

- **Unit:** discovery enqueues and advances per page; per-item collapse
  (change-then-remove); newest-first ordering; backoff and no head-of-line blocking;
  a `version` change resets `attempts`.
- **`EXPLAIN QUERY PLAN` on `due_sync_items`**, asserting `SEARCH … USING INDEX`, run
  against an **already-initialised** store. This is the 0.7.105 + `test_metadata_jsonb`
  lesson: every prior index test built a *fresh* store, where DDL and query text
  necessarily agree, so none could catch drift on an existing store.
- **Atomicity:** fail between the chunk write and completion; assert the item is still
  queued and no chunks are orphaned.
- The 0.7.123 livelock tests (`tests/test_drive_paging_resume.py`) are rewritten against
  the new shape; they should pass by construction.
- **Live validation** against the real feed in a throwaway store: discovery reaches the
  feed head and enqueues, with the durable cursor advancing.
- **Gold gate before/after: recall@10 ≥ 0.780, MRR ≥ 0.550** — non-negotiable, since this
  changes what lands in the corpus.
