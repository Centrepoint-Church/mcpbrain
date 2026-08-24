# Ingestion resilience + tabular rendering — design

**Date:** 2026-08-24
**Status:** approved direction, pending implementation plan
**Origin:** `mcpbrain doctor` flagged two warnings on the author's live install —
backup upload failing (4d stale) and a chunk-window notice (5,181 chunks over the
2,000-char embedder window). Root-cause investigation surfaced four independent
defects, not one. A follow-up question ("is multi-chunk item ingestion — doc
edits, thread growth — handled well?") led to re-verifying five older findings
(B4-B8 from `2026-07-27-ingestion-defects-findings.md`) against current code —
all five turned out to already be fixed — and surfaced one genuinely new gap
in how short threads get prior-message context (section 5). A final "is this
actually the best/most complete fix, not a v1" pass then found two of the
four original sections were narrower than they should be: the backup retry
gap turned out to affect ~40 call sites codebase-wide, not 2, and the
tabular-rendering fix needed a root-cause change in `normalise_rows` itself
(section 2a-0), not just a more robust renderer downstream of it. This spec
covers all five items at that depth.

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
   cooldown circuit breaker). Later widened, also confirmed with the user:
   the retry layer applies codebase-wide (~40 call sites), not just backup's.
2. Tabular — redesign rendering to schema-enriched row sentences, AND stop
   embedding table-subtype content into the dense vector index (keep FTS).
   Later deepened: also fix the width computation at its root in
   `normalise_rows`, not only in the renderer that consumes it.
3. Consolidated notes — route through `chunk_text()`, bounded like every
   other multi-chunk source.
4. Repair tooling — generalize `reingest-stale` to cover every source type,
   not just Drive.

---

## 1. Retry gap: not just backup — 40 call sites codebase-wide

**Revised scope (confirmed with user):** the original report was backup-only.
Auditing every `.execute()` call in the codebase found `num_retries` passed
at only 4 of ~44 sites — `sync/gmail.py` and `sync/attachments.py` already
have their own local `_NUM_RETRIES = 5` (correctly applied), and
`backup.py`'s resumable media upload has a deliberate, well-commented
`_MEDIA_NUM_RETRIES = 0` (a retried chunk can't re-seek a non-seekable
stream — do not touch this one). Every other call — `backup.py`'s *other*
~10 calls (including `prune_snapshots`, which runs right after every upload
and was equally exposed to the exact bug that was reported, but which the
original narrower fix would have missed entirely), all of `fleet.py`, all of
`dashboard.py`, `auth.py`'s `userinfo().get()`, and `sync/drive.py`'s main
sync path — has no retry at all, so every one of them is equally exposed to
the same `Errno 49`-class transient failure, not just backup's.

**Files:** `mcpbrain/backup.py`, `mcpbrain/fleet.py`, `mcpbrain/dashboard.py`,
`mcpbrain/auth.py`, `mcpbrain/sync/drive.py`, `mcpbrain/sync/calendar.py`,
`mcpbrain/daemon.py` (`maybe_backup`, `_build_drive_service`).

**Design — two independent layers:**

1. **Per-call retry, everywhere, via the existing idiom.** `googleapiclient`'s
   own `.execute(num_retries=N)` already implements randomized exponential
   backoff and already retries exactly this error class (SSL errors, socket
   timeouts, `ConnectionError`, and `OSError` generally — confirmed against
   the library source, `_retry_request` in `googleapiclient/http.py`). No new
   retry logic needs to be written. The fix is mechanical: add a local
   `_NUM_RETRIES = 5` constant to every module above that's missing one
   (matching the exact value and pattern already established in `gmail.py`
   and `attachments.py` — a new shared cross-module constant would be less
   idiomatic than extending the convention that's already there), and pass
   `num_retries=_NUM_RETRIES` on every non-resumable `.execute()` call in
   that module. `backup.py` gets both constants side by side —
   `_MEDIA_NUM_RETRIES = 0` (unchanged, still commented as deliberate) and a
   new `_NUM_RETRIES = 5` for every other call including `prune_snapshots`'s.
2. **Rebuild-on-persistent-failure, backup only.** `daemon.maybe_backup`
   already tracks `consecutive_failures` via `write_backup_state`. Add: after
   a backup attempt fails AND `consecutive_failures >= 2` (two full-interval
   misses, matching the existing "single failure is routine" philosophy
   already documented at `probes.py:168`), rebuild `drive_service` via
   `_build_drive_service()` and swap it into `self._backup.drive_service`
   under `_config_lock` before returning. This is a second, smaller safety
   net for the case where the client/session itself is persistently broken
   in a way layer-1's per-call retry can't paper over (e.g. a stale/invalid
   token) — with layer 1 in place codebase-wide, this should fire far less
   often than the 4 outages that motivated it originally, but it closes the
   "only a full daemon restart fixes it" gap for whatever's left.
   `BackupConfig.drive_service` is already a plain, reassignable dataclass
   field — no schema change needed.

**Testing:** for each touched module, a unit test asserting its `.execute()`
calls pass `num_retries=_NUM_RETRIES` (grep-style assertion against the call,
or a fake `http` that records `num_retries` on the request object). Unit
test `maybe_backup`'s rebuild trigger: fake `drive_service` raising twice,
assert `_build_drive_service` (monkeypatched) gets called and the daemon's
`_backup.drive_service` identity changes.

---

## 2. Tabular rendering: schema-enriched rows + skip dense embedding

**Files:** `mcpbrain/sync/tabular.py` (`render_chunks`, `_fit_row`, `_md_row`,
`normalise_rows`), `mcpbrain/index.py` (`index_pending`), `mcpbrain/store.py`
(`write_embedding`, `unembedded_chunks`).

### 2a-0. Root-cause fix in `normalise_rows` (defense in depth, not just the renderer)

The renderer redesign below (2a) makes the *output* immune to a phantom-width
`Table`, but `normalise_rows()` itself is still where the phantom width gets
created, and it feeds more than just the renderer — `_summary_text` (the
per-table "summary" chunk emitted first) loops `for i, name in
enumerate(t.header): for r in t.rows: ...`, an O(width × rows) cost that
scales with the SAME inflated width (a genuinely wide phantom-column sheet
would make this loop slow, independent of the chunk-size bug). Fixing this
one level down closes both problems at the source and benefits every current
and future consumer of `Table`, not just `render_chunks`.

Current logic trims trailing columns using the MAX non-empty column index
across every row — exactly what one anomalous row (the reproducing case: a
title banner with a single stray non-empty cell far to the right) defeats.
The fix is **not** switching to a median (a legitimately-sparse-but-real
trailing column used by a genuine minority of rows — e.g. a "notes" column
only some invoice rows populate — could get silently dropped if fewer than
half the rows use it). Instead, require **minimum multi-row support**: a
column only counts as real if at least `max(2, len(kept) // 100)` distinct
rows have non-empty content there (tune the exact fraction during
implementation against real fixture data). One outlier row can never clear a
support threshold of 2 on its own, while a column used by even a modest
minority of rows still counts as real. Falls back to today's behavior (no
column clears the threshold — e.g. a genuinely tiny 1-2 row table) so a small
legitimate table isn't wrongly trimmed to zero width.

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

**Testing:** unit test `normalise_rows` against the exact reproducing case
(a title-banner row with one stray non-empty far-right cell among hundreds
of normal-width rows) asserting the outlier no longer inflates width, PLUS a
case with a legitimately-sparse real column (used by a minority but more
than the support floor of rows) asserting it's preserved, not dropped. Unit
tests for the new renderer against the same reproducing case, asserting
output stays under `CHUNK_CHARS` regardless (belt-and-suspenders even after
2a-0's fix). Unit test the field-count safety valve. Unit test
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

**Why this has to be a write-time cap, not just a prompt fix:** the
consolidation prompt (`consolidation.py:_PROMPT_TEMPLATE`) already asks for
"a concise durable semantic note (3-6 sentences)" — and 1,151 notes still
average 18K chars anyway. The prompt instruction is evidence, not a
guarantee; an LLM asked for 3-6 sentences over a large/diverse source
cluster can still produce far more (e.g. trying to cite every source
individually). This is exactly why the fix has to be defensive at the write
layer regardless of prompt wording — consistent with every other bound in
this codebase (never trust upstream generation to self-limit).

**Testing:** unit test `_write_note` with a summary long enough to require
2+ chunks — assert multiple doc_ids written, each under budget, and that a
short summary still produces exactly the current single-doc_id shape
(no regression for the common case).

---

## 4. Generalize the re-chunk repair sweep across source types

**Files:** `bin/repair.py` (`phase_reingest_stale`), `mcpbrain/store.py`
(`stale_chunker_file_ids` → generalized), `mcpbrain/sync/gmail.py` (new
`reingest_messages`, mirroring the existing `reingest_files` shape),
`mcpbrain/sync/calendar.py` (reuse `backfill_calendar_window`, no new
function needed).

**Store layer:** `store.stale_chunker_file_ids(CHUNKER_VERSION, limit)` is
Drive-specific by name and by query (`doc_id LIKE 'gdrive-%'` implied by its
existing use). Generalize to `store.stale_chunker_ids(CHUNKER_VERSION,
limit) -> list[dict]`, returning `{"source_type": "gdrive"|"gmail"|
"calendar", "id": <file_id|thread_id|event_id>}` per stale item, grouped by
owning file/thread/event (not per-chunk) exactly as the Drive version
already does. Process sequentially by source type (Drive, then Gmail, then
Calendar) rather than interleaving — the tool is already designed to be
re-run repeatedly and safely (no cursor to disturb, per `reingest_files`'s
own docstring), so successive runs naturally make cross-source progress
without needing round-robin scheduling; that's added complexity this
one-time-ish sweep doesn't need.

**Dispatch, mirroring the existing Drive path exactly:**
- `gdrive` → `reingest_files` (unchanged, existing behavior).
- `gmail` → new `sync/gmail.py::reingest_messages(service, store,
  message_ids, ...)`, built the same shape as `reingest_files`: per message,
  `_fetch_one` (already exists, already thread-safe, already retries via its
  own `_NUM_RETRIES`) → re-chunk → `upsert_email_context`/chunk write. Unlike
  Drive, an email's content never shrinks after the fact (immutable once
  sent), so there's no B5-style orphan-sweep needed here — this is purely
  "re-chunk under the current chunker version," simpler than the Drive case.
  **Must still replicate `reingest_files`'s convergence guard**: a 404'd
  message (deleted/inaccessible) gets its existing chunks stamped with the
  current `chunker_version` anyway, exactly like Drive's `missing`/`empty`
  outcomes — without this, a permanently-inaccessible message gets
  re-selected and re-fetched on every single run forever (this is a measured,
  not hypothetical, failure mode: Drive's own reingest hit exactly this,
  ~46 repeat fetches of the same 10 files in 41 minutes, before the stamp-on
  non-convergent-outcome guard was added).
- `calendar` → reuse `backfill_calendar_window` scoped to a narrow window
  bracketing the stale event's own start time, rather than building a new
  targeted single-event-refetch primitive. Calendar's stale-chunk volume is
  tiny (4, in the original investigation) — not worth a bespoke function for.
- `note-*` chunks stay out of scope for this sweep (regenerated by
  consolidation, not re-fetched from a source) — section 3 is what bounds
  them going forward.

**Sequencing constraint (unchanged from the original draft):** this must
ship *before or alongside* section 2a's `CHUNKER_VERSION` bump — bumping the
version with no generalized sweep in place would mark every non-Drive chunk
stale with nothing able to act on it, which is harmless (they just wait) but
pointless to sequence that way. Land section 4 first (pure generalization,
no behavior change until a version bump happens), then land 2a's version
bump on top.

**Testing:** extend `bin/repair.py`'s existing test coverage with a
Gmail-sourced stale-message fixture, asserting it gets re-queued through the
new `reingest_messages` the same way a Drive fixture goes through
`reingest_files` today, plus a Calendar fixture asserting the narrow-window
`backfill_calendar_window` call.

---

## 5. Short threads get no prior-message context

### What's already fine (verified against current code, not assumed)

Re-checking B4-B8 from the 2026-07-27 findings doc against current code found
all five already fixed:

- **B4** (thread expansion scrambled paragraph order) — `retrieval_expand._by_date`
  now sorts by `(date, message_id, chunk_index)`, not date alone.
- **B5** (stale chunks orphaned on doc shrink) — `drive.py::upsert_file_chunks`
  diffs the current extraction's doc_ids against `store.doc_ids_for_file` and
  deletes orphans via `store.delete_chunks` (clears `vec_chunks`+`fts_chunks`
  too), with a guard that skips the delete entirely on a partial/failed
  extraction so a transient error can't be mistaken for a shrink.
- **B6** (`chunk_text` emitting empty/oversize chunks) — the zero-length-chunk
  path is guarded (`if current: chunks.append(current)` before splitting an
  oversized paragraph) and the final return filters empties.
- **B7** (silent `except Exception: return ""` in extractors) — every
  extraction exception now logs (`log.debug`/`log.warning`) with the actual
  error, and `PartialTables` distinguishes a partial failure from "no
  content" so B5's orphan-sweep doesn't misfire on it.
- **B8** (partial document presented as whole to the model) — `_join_with_gaps`
  explicitly inserts a gap marker for a missing chunk_index and a truncated-tail
  marker when `chunk_total` is known, so a partially-enriched or cold-excluded
  thread is never silently presented as complete.

No action needed on any of the above — noted here so this spec is the single
place that confirms it, rather than leaving five findings looking "open" in
an old doc that current code has already resolved.

### The new gap: prior-message context is empty for threads under 5 messages

**Files:** `mcpbrain/prepare.py` (`_thread_block`), `mcpbrain/store.py`
(new method), `mcpbrain/synthesise_threads.py` (`build_synthesis_requests`,
refactor only).

`thread_context.contextual_summary` — the field `prepare._thread_block` reads
into `prior_thread_context` for a growing thread's next enrichment pass — is
populated **only** by the periodic cross-message synthesis pass
(`synthesise_threads.py::drain_synthesis`). `graph_write.apply()` deliberately
never writes it (its own comment: "left unset here for the deeper synthesis
pass to fill"). Synthesis itself only considers threads with `email_count >=
min_emails` (default 5, in `build_synthesis_requests`/`threads_needing_summary`).
So a thread with fewer than 5 messages — the common case — gets a genuinely
empty `prior_thread_context` on every subsequent message, even though every
prior message's own one-line `summary` is already durably stored per-message
in `email_context` (via `upsert_email_context`, written on every `apply()`
regardless of thread length) and is fully queryable today via
`store.thread_messages(thread_id)` — the exact same data
`build_synthesis_requests` already reads to build its digest for the ≥5 case.

This is a pure read-side gap: no writer needs to change, no new accumulation
or overwrite-ordering logic is needed (there is no overwrite bug — `apply()`
simply never touches `contextual_summary` at all), and nothing about the
existing synthesis pass's behavior or the `thread_context` table's semantics
needs to change.

**Fix:** add `store.thread_summary_digest(thread_id, max_chars=1500) -> str`:
reads `email_context` rows for the thread ordered by `date_iso`, joins each
as `f"- {date_iso}{content_type_tag}: {summary}"` (identical line format to
`build_synthesis_requests`'s existing inline loop), and caps the joined
result to `max_chars`, dropping the **oldest** lines first when it doesn't
fit (the most recent messages are the most relevant prior context for
whatever's about to be enriched next). In `prepare._thread_block`, when
`store.thread_context(thread_id)` returns an empty `contextual_summary`
(true for any thread not yet synthesized — short or simply not-yet-due),
fall back to `store.thread_summary_digest(thread_id)` instead of `""`. Once
a thread crosses the synthesis threshold and gets a real narrative, that
takes over exactly as today — this fallback only fills the gap before that
point, for the threads that never reach it, and does not compete with or
get overwritten by anything.

**DRY refactor (optional, no behavior change):** `build_synthesis_requests`'s
existing inline per-message-summary-join loop becomes redundant with the new
method — have it call `store.thread_summary_digest(thread_id, max_chars=None)`
instead, so the line-format logic lives in exactly one place.

**Testing:** unit test `thread_summary_digest` against a fixture thread with
several `email_context` rows, asserting line format, ascending-date order,
and that exceeding `max_chars` drops the oldest lines, not the newest. Unit
test `_thread_block`'s fallback: `thread_context` empty → digest used;
`thread_context` non-empty (post-synthesis) → digest never called, existing
value used unchanged.

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
