# Ingestion resilience + tabular rendering — design

**Date:** 2026-08-24
**Status:** approved direction, pending implementation plan
**Origin:** `mcpbrain doctor` flagged two warnings on the author's live install —
backup upload failing (4d stale) and a chunk-window notice (5,181 chunks over the
2,000-char embedder window). Root-cause investigation surfaced four independent
defects, not one. This spec covers all four.

## Context

Investigating the two doctor warnings live (see chat transcript, 2026-08-24)
found:

1. **Backup uploads fail every few days** with `[Errno 49] Can't assign
   requested address` at the very first Drive network call in
   `backup.py:upload_snapshot` (the folder lookup). A daemon restart fixed it
   immediately (confirmed: `backup_state.json` shows a success 6 minutes after
   restart). A prior spec (`2026-07-27-daemon-scheduling-design.md`) already
   documented this exact error signature and explicitly deferred a fix.
2. **5,181 chunks exceed the 2,000-char embedder window**, and this splits into
   three unrelated causes on inspection:
   - **3,510 chunks (avg 150K–198K chars, max 293KB)**: a spreadsheet-rendering
     bug. `tabular.normalise_rows()` computes trailing-empty-column trim as one
     global max width across an entire sheet, so a single anomalous row (a
     title banner, a stray far-right formatted-but-blank cell — both common
     in real Excel files) inflates every other row's rendered width. The
     per-cell shrink safety valve (`_fit_row`) can't compensate because it
     reduces cell *width*, not column *count*.
   - **1,151 chunks (avg 18K chars)**: `consolidation.py::_write_note` writes
     an LLM summary directly to the store with no length cap and no
     `chunk_text()` call — the only content-write path in the ingestion
     system with no bound at all.
   - **~600 chunks (avg 4-5K chars)**: legacy chunks that predate the
     2026-07-28 chunker headroom fix. Drive has a repair tool
     (`bin/repair.py reingest-stale`) that re-chunks stale files under the
     current `chunker_version`; Gmail/calendar have no equivalent, so these
     can never self-heal.

All oversize chunks are `embedded=1` — they went through the embedder, which
silently truncates at ~2,000 chars, so a 293KB tabular chunk contributes a
near-duplicate low-quality vector (title text + garbage) to the corpus.

External research (chunking-strategy literature for RAG, Google Cloud client
library guidance, Excel/openpyxl used-range behavior — see prior chat message
for citations) confirmed: (a) schema-enriched row-sentences outperform raw
markdown-grid table embeddings for retrieval, (b) the standard resilience
pattern for a long-lived API client is bounded retry + rebuild-on-persistent-
failure, not a single try/except with no recovery, and (c) Excel's "used
range" is routinely inflated by formatting alone, so trusting a computed
global width is fragile by construction.

**Decisions locked in with the user:**
1. Backup — retry + rebuild-on-persistent-failure (not retry-only, not a
   cooldown circuit breaker).
2. Tabular — redesign rendering to schema-enriched row sentences, AND stop
   embedding table-subtype content into the dense vector index (keep FTS).
3. Consolidated notes — route through `chunk_text()`, bounded like every
   other multi-chunk source.
4. Repair tooling — generalize `reingest-stale` to cover every source type,
   not just Drive.

---

## 1. Backup: bounded retry + rebuild-on-persistent-failure

**Files:** `mcpbrain/backup.py` (`upload_snapshot`, folder lookup/create
calls), `mcpbrain/daemon.py` (`maybe_backup`, `_build_drive_service`).

**Design:**
- Add a small retry helper (module-level in `backup.py`, not a new
  dependency) that wraps the two `service.files().list()/.create()` calls in
  `upload_snapshot`'s folder-resolution step (lines ~773-798) with up to 3
  attempts, exponential backoff (1s / 2s / 4s), retrying only on
  `OSError`/socket-level errors (`Errno 49`, `Errno 32` broken pipe, etc.) —
  not on 4xx auth/permission errors, which retrying can't fix.
- **Do NOT touch the chunked media upload's `num_retries=0`** — that's a
  deliberate, documented constraint (a retried chunk can't re-seek a
  non-seekable stream). This retry applies only to the plain idempotent
  metadata calls that precede it.
- `daemon.maybe_backup` already tracks `consecutive_failures` via
  `write_backup_state`. Add: after a backup attempt fails AND
  `consecutive_failures >= 2` (two full-interval misses, matching the
  existing "single failure is routine" philosophy already documented at
  `probes.py:168`), rebuild `drive_service` via `_build_drive_service()` and
  swap it into `self._backup.drive_service` under `_config_lock` before
  returning. The next scheduled attempt gets a fresh client without needing
  a manual daemon restart.
- `BackupConfig` needs `drive_service` to be assignable post-construction (it
  currently is — a plain dataclass field) — no schema change needed, just the
  rebuild-and-reassign call site in `maybe_backup`'s except branch.

**Testing:** unit test the retry helper against injected `OSError` sequences
(succeeds on 2nd/3rd attempt, gives up after 3, does not retry a
non-transient exception). Unit test `maybe_backup`'s rebuild trigger: fake
`drive_service` raising twice, assert `_build_drive_service` (monkeypatched)
gets called and the daemon's `_backup.drive_service` identity changes.

---

## 2. Tabular rendering: schema-enriched rows + skip dense embedding

**Files:** `mcpbrain/sync/tabular.py` (`render_chunks`, `_fit_row`, `_md_row`,
`normalise_rows` stays as-is — the *global* width computation is what's being
removed, not the trim itself), `mcpbrain/index.py` (`index_pending`),
`mcpbrain/store.py` (`write_embedding`, `unembedded_chunks`).

### 2a. Rendering redesign

Replace the fixed-width markdown-grid renderer with schema-enriched row
sentences. Each row renders independently — no shared `width`/`header_line`/
`sep_line` across the sheet, so one anomalous row can never inflate another
row's output:

```
### Sheet: Fixed Assets Register 2022 — row 15 of 717
Item: Chairs (stackable); Cost: 1,200.00; Purchased: 2022-03-14; Location: Hall B
```

- For each row, emit `"{header[i]}: {value[i]}"` pairs for cells where
  `value[i].strip()` is non-empty — an empty cell is simply never rendered,
  which is what makes this immune to phantom trailing columns by
  construction (no need to compute a "safe" width at all).
- Per-value elision stays: reuse the existing `_cell()`/`_MAX_CELL_CHARS`
  (300 chars) truncation for one runaway long value.
- New safety valve replacing `_fit_row`'s cell-shrink loop: if a row has more
  than `_MAX_FIELDS_PER_ROW` (new constant, default 40 — generous for a real
  spreadsheet, tight enough to bound a phantom-column sheet) non-empty
  fields, keep the first `_MAX_FIELDS_PER_ROW` and append `"(+N more
  fields)"`. This is the direct replacement for the old cell-width-shrink
  valve, and it bounds the pathological case (thousands of phantom non-empty
  cells, if that ever happens) at the field-count level instead of the
  character-width level.
- Multiple rows still pack into one chunk up to `CHUNK_CHARS` (1800) via the
  same rolling-accumulation loop as today (`group`, flush-on-overflow), just
  measuring the new row-sentence text instead of a markdown line. Delete
  `_md_row`, `_fit_row`, and the `width`/`header_line`/`sep_line` computation
  entirely — they have no role in the new design. `_rendered_size`,
  `_title`, `_summary_text`, `_emit`'s shape (title + joined lines) stay.
- Metadata (`table_role`, `row_start`, `row_end`, `rows_total`, `truncated`,
  `sheet`) is unchanged — only the `text` payload construction changes.

### 2b. Skip dense embedding for table-subtype content, keep FTS

`content_subtype == "table"` chunks (Drive spreadsheets/CSV, gmail
spreadsheet attachments — the same tag the salience gate already reads) stop
going through the dense-vector embedder. They keep full FTS indexing so
exact-match lookups ("find the line item for the projector") still work —
only the weak, research-disfavored raw-table dense embedding is dropped.

**Important implementation detail:** `write_embedding()` currently couples
computing+storing the vector with computing+storing the FTS row in one call
— `upsert_chunk()` never touches `fts_chunks`. Skipping embedding entirely
for table chunks would therefore also skip their FTS population, which is
NOT the goal. Fix: extend `write_embedding(rowid, vector, *, home=None)` to
accept `vector=None`, meaning "still compute/write the FTS row and stamp
`embedded=1`, but skip the `vec_chunks` insert." `embedded=1` with no
`vec_chunks` row is safe everywhere else in the codebase that gates on
`embedded=1` for vector search — a join against `vec_chunks` simply returns
nothing for that rowid, which is exactly the desired "never surfaces via
dense search" behavior, while `unembedded_chunks()` correctly stops
re-fetching it every cycle.

`index_pending`'s batch loop partitions each batch into table-subtype and
normal chunks before calling `embedder.embed_passages()`: normal chunks go
through the embedder as today; table chunks skip the embed call (saving
embedder compute) and call `write_embedding(rowid, None)`.

Gate behind a new flag `embed_skip_tabular` (default **OFF**), following this
project's own established rollout discipline (`salience_gate`,
`recall_excludes_cold` were both validated on the live gold-eval harness
before defaulting on). Flip to ON only after confirming recall@10/MRR are
unaffected (tabular content was never a strong recall contributor per the
existing salience-gate evidence, but verify rather than assume).

### 2c. One-time cleanup of existing garbage vectors

Bump `CHUNKER_VERSION` (currently 2 → 3) as part of shipping 2a — this is
the established mechanism for "re-chunk everything under the new logic"
(the same mechanism used for the 2026-07-28 headroom fix). This
deliberately marks not just the affected tabular chunks but every chunk
below the new version as stale, which is correct, not wasteful: it unifies
this rollout with section 4's generalized repair sweep, so the SAME sweep
re-fetches and re-renders (a) the tabular chunks broken by the phantom-
column bug, AND (b) the ~600 legacy pre-headroom-fix Gmail/calendar chunks
from the original investigation — one mechanism, two birds.

Because the garbage vectors from the 3,510 existing oversize tabular chunks
would otherwise keep polluting dense search until the full re-fetch sweep
drains (which runs at the daemon's normal backfill pace, not instantly),
also add a small one-shot script (matching this codebase's convention:
`bin/relocate_ingest_cache.py`, `bin/consolidate.py` — dry-run default, an
`--apply` flag to commit) that immediately deletes `vec_chunks` rows for
chunks where `content_subtype == "table" AND length(text) > 2000`
(`DELETE FROM vec_chunks WHERE rowid IN (...)`), so the garbage vectors stop
being returned by dense search right away, ahead of the version-bump sweep
re-rendering their `text` from scratch.

**Testing:** unit tests for the new renderer against the exact reproducing
case (a table with a header at column 0 and one anomalous row with a
non-empty value far to the right) asserting output stays under `CHUNK_CHARS`
regardless. Unit test the field-count safety valve. Unit test
`write_embedding(rowid, None)` writes `fts_chunks` and stamps `embedded=1`
without touching `vec_chunks`. Unit test `index_pending`'s batch
partitioning (table chunks never reach `embed_passages`).

---

## 3. Consolidated notes: bound via `chunk_text()`

**Files:** `mcpbrain/consolidation.py` (`_write_note`).

Replace the direct `store.upsert_chunk(doc_id, text, ...)` single-blob write
with the same multi-chunk pattern every other source uses: if
`chunk_text(summary)` returns more than one piece, write
`note-consolidated-<hash>-<i>` for each, matching the existing
`gdrive-<fid>-<i>` / `gmail-...-body-<i>` doc_id convention (metadata
`chunk_index`/`chunk_total` fields, same as `drive.py`'s `Chunk` construction
at `drive.py:346`). Single-chunk case (the common one) keeps today's bare
`note-consolidated-<hash>` doc_id unchanged — no migration needed for
existing single-chunk notes.

**Testing:** unit test `_write_note` with a summary long enough to require
2+ chunks — assert multiple doc_ids written, each under budget, and that a
short summary still produces exactly the current single-doc_id shape
(no regression for the common case).

---

## 4. Generalize the re-chunk repair sweep across source types

**Files:** `bin/repair.py` (currently Drive-only `reingest-stale`).

Replace the Drive-specific stale query with a generic one keyed only on
`chunker_version < CHUNKER_VERSION` (already the correct predicate — just
not applied outside `doc_id LIKE 'gdrive-%'` today), dispatching the re-fetch
by doc_id prefix: `gdrive-*` → `backfill_drive` (existing behavior,
unchanged), `gmail-*` → `backfill_gmail` for the owning thread, `cal-*` →
`backfill_calendar_window` for the owning event's window. `note-*` chunks are
out of scope for this sweep (they're regenerated by consolidation, not
re-fetched from a source) — section 3's fix is what bounds them going
forward.

This must ship *before or alongside* section 2a's `CHUNKER_VERSION` bump —
otherwise bumping the version marks every chunk stale with no sweep able to
act on the non-Drive ones yet, which is harmless (they just wait) but
pointless to sequence that way. Implementation order: land section 4 first
(pure generalization, no behavior change until a version bump happens),
then land 2a's version bump on top.

**Testing:** extend `bin/repair.py`'s existing test coverage with a
Gmail-sourced stale chunk fixture, asserting it gets re-queued through
`backfill_gmail` the same way a Drive fixture goes through `backfill_drive`
today.

---

## Non-goals / explicitly out of scope

- Re-deriving whether the salience gate's tabular cold-marking threshold is
  correct — unrelated axis (graph-extraction eligibility, not embedding
  eligibility) and already validated live.
- Migrating existing single-chunk `note-consolidated-*` records to a
  multi-chunk shape — only new writes going over budget get the multi-chunk
  treatment.
- A circuit-breaker/cooldown window for backup (option C from the earlier
  brainstorm) — bounded retry + rebuild-on-persistent-failure was the chosen
  option; a cooldown is unnecessary complexity for an hourly cadence.
- Full LlamaParse/Docling-style table ETL — the schema-enriched row-sentence
  redesign is the right-sized fix for this corpus; a dedicated table-parsing
  service is out of proportion to the problem.
