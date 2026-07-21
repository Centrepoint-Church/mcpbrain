# Drive-doc enrichment matching fix

**Date:** 2026-07-21
**Status:** approved, implementing

## Problem

Drive documents are effectively never enriched into the graph via the
thread-enrich drain path. On the live store this produced **11,782
"matched no chunk" drain warnings, 11,185 (95%) of them Drive docs**, with
**85,412 Drive chunks stuck `enriched=0`**, re-queuing every cycle and burning
Haiku calls on extractions that can never apply.

### Root cause: three components disagree on a Drive chunk's identity

A Drive chunk's metadata carries `file_id` (e.g. `1Tj2…`) but **no
`message_id`**, and its `doc_id` is `gdrive-<file_id>-<chunk_index>`.

| Component | Drive key used | Value for chunk `…-764` |
|---|---|---|
| `_group_key` (batching, `thread_enrich.py`) | `thread_id → message_id → **doc_id**` | `gdrive-1Tj2…-764` |
| `reassemble_thread` (`thread_enrich.py`) | **`file_id`** | `1Tj2…` |
| `doc_ids_for_messages` (`store.py`) | `metadata.message_id`, else `doc_id` | resolves neither `1Tj2…` |

Flow for each Drive chunk today:
1. Batching makes it its **own** thread, `thread_id = doc_id = gdrive-1Tj2…-764`.
2. `reassemble_thread` emits the extraction message with `message_id = file_id
   = 1Tj2…` (bare).
3. In drain, `doc_ids_for_messages(["1Tj2…"])` matches nothing — no chunk has
   `metadata.message_id`, and no chunk's `doc_id` equals the bare `file_id`.
   Both fallbacks reuse the same file_id-keyed messages, so they miss too.
4. `drain.py` logs "matched no chunk", skips the apply, bumps the attempt cap.
   After `_EMPTY_ATTEMPT_CAP` (3) tries it would `mark_enriched` with **zero
   edges** (the "gave up" path).

### No data remediation required (fix-forward)

Live store at diagnosis: 85,412 Drive chunks `enriched=0`, 743 `enriched=1`.
The give-up path only marks at `enrich_attempts ≥ 3`; **zero** enriched Drive
chunks have `attempts ≥ 3` (0→682, 1→51, 2→10). The 743 were enriched via a
different, working path (ingest/cache — already 119 `gdrive-` graph edges). So
nothing has been falsely marked-enriched-without-edges; once the drain path
works, the stuck chunks flow correctly. No migration/reset.

## Design

Make all three components agree on **`file_id` as the Drive doc's identity**.

### Change 1 — `_group_key` (`mcpbrain/thread_enrich.py`)

For a chunk with a `file_id` and no `thread_id`, group by `file_id` instead of
`doc_id`, so a whole Drive doc becomes **one batch → one reassembled message**
(matching `reassemble_thread`'s existing file_id grouping and stated intent).
Priority mirrors `reassemble_thread`: `thread_id → file_id → message_id →
doc_id`. The group key is the bare `file_id`, so batch `thread_id ==`
reassemble's `message_id`, keeping drain's thread_id fallback trivially correct.

### Change 2 — `doc_ids_for_messages` (`mcpbrain/store.py`)

Add a resolution branch: a passed id that equals a chunk's `metadata.file_id`
resolves to **all** chunks of that file, ordered by rowid. This is the missing
bridge from `message_id (= file_id)` back to the doc's chunks. Collision-safe:
Drive file_ids (~33-char base64) do not look like Gmail message/thread ids
(16-hex or RFC822), so the new branch only fires for genuine Drive file_ids.

### Why this is complete and exact

Batch `doc_ids` (all the file's chunks) `==` what `doc_ids_for_messages(file_id)`
returns `==` what receives `mark_enriched`. No over- or under-marking. Large
PDFs collapse into one thread that `prepare._split_long_thread` shards into
parts sharing the file_id thread; drain re-groups the parts by `thread_id` and
applies once (chosen granularity: whole doc = one thread).

## Testing (TDD)

Write failing tests first:

1. **Unit — `doc_ids_for_messages`:** a store with N chunks of one Drive file
   (`doc_id = gdrive-<fid>-i`, `metadata.file_id = <fid>`, no `message_id`);
   `doc_ids_for_messages([fid])` returns all N doc_ids in rowid order. Also
   assert email resolution (by `message_id`) and the doc_id fallback still work
   (no regression).
2. **Unit — `_group_key`:** a Drive chunk groups under its `file_id`; multiple
   chunks of one file land in one batch; an email chunk still groups by
   `thread_id`.
3. **Integration — drain:** a multi-chunk Drive batch applies (entities/relations
   written) and marks all its chunks `enriched=1`, with **no** "matched no
   chunk" warning.

Then run the affected test modules (`tests/` for `thread_enrich`, `store`,
`drain`) plus the gold-eval gate; confirm recall@10 / MRR hold before shipping.

## Out of scope

- The 597 non-Drive "matched no chunk" misses (some `cal-…`, some email) — a
  smaller, separate tail to investigate later.
- Any change to the ingest/cache enrichment path (already working).
- Release/ship — a separate explicit step per project rules.
