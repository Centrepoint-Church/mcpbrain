# Extraction fidelity: structure-preserving extraction, a line-aware chunker, and a content-preserving reflow

Date: 2026-09-24
Status: design approved, not implemented

## Problem

Documents reach the chunker with their structure already gone, and the chunker
then destroys what little is left.

**The chunker.** `chunking.chunk_text` splits only on blank lines (`\n\n`).
Every paragraph larger than the budget goes to `_split_paragraph`, which
word-splits via `para.split()` (`chunking.py:267`) — collapsing every newline in
that paragraph to a single space. Our extractors, and Google's `text/plain`
export, separate paragraphs with a single `\n`, so almost every document arrives
as ONE paragraph and is word-split end to end.

Measured on the live store (random 3,000-chunk samples of Drive prose,
2026-09-24):

| Source        | chunks with no blank line | median chunk length |
|---------------|---------------------------|---------------------|
| DOCX          | 94%                       | 1,797 (= the budget) |
| Google Docs   | 100%                      | 1,796               |
| PDF           | 88%                       | 1,554               |

A sampled DOCX chunk (an internal review form) had **zero newlines**: questions,
answers and row numbers flattened into one run of text. Chunks begin and end
mid-sentence; headings run into body text; lists, addresses and table rows are
merged into lines. Long Gmail bodies are hit the same way: of sampled body
chunks near the budget, 97% contain no newline.

**The extractors** (`mcpbrain/sync/extractors.py`, `drive.py`):

- **PDF** — `page.get_text()` with no `sort=True`: text in content-stream order,
  so multi-column layouts can interleave. `find_tables()` is not used; PDF
  tables come out as loose tokens.
- **DOCX** — paragraphs joined with a single `\n`; ALL tables appended after all
  paragraphs (out of context); empty cells dropped (columns misalign); merged
  cells repeated (python-docx returns the merged cell once per spanned grid
  position); heading styles, headers/footers and text boxes ignored.
- **PPTX** — speaker notes never read; shapes inside groups skipped.
- **RTF** — fetched as bytes and `decode("utf-8")`'d (`drive.py:172`), so the
  control words (`{\rtf1\ansi\deff0…`) are chunked and embedded. Zero RTF files
  on the author's store; the defect is real for any other install.
- **Google Docs/Slides** — exported as `text/plain`, which discards headings,
  lists and tables before we ever see them.

**Scale on the author's store:** ~5,300 Drive files and ~1,600 Gmail messages
have at least one word-split chunk (~95,000 chunks). ~70,000 of the affected
PDF/DOCX/Google-Docs chunks are already **enriched**.

## Goals

1. Every extractor preserves document structure: reading order, paragraphs,
   headings, tables in position, notes and secondary text.
2. The chunker never collapses a line break, and splits at the largest natural
   boundary available.
3. The existing corpus is re-chunked **without discarding enrichment it already
   paid for**, and without breaking any provenance, citation or gold case.
4. The backfill runs inside the daemon (the single writer), resumably, on every
   install, without starving normal sync.
5. Gold gate holds: recall@10 ≥ 0.850 and MRR not below 0.546 (both the
   2026-09-10 measurement) after the backfill completes.

## Non-goals

- Replacing pymupdf / python-docx / python-pptx with anydoc or pdf-inspector.
  Both were evaluated (2026-09-24): anydoc's OCR path is a cloud service
  (unacceptable for this data) and it renders tables as Markdown grids, which
  the v3 chunker deliberately replaced; pdf-inspector is reconsidered ONLY if a
  fixture below shows pymupdf's `sort=True` still interleaves columns.
- Changing the spreadsheet/table pipeline (`sync/tabular.py`) or anything
  `CHUNKER_VERSION` 3 governs.
- Changing OCR. The per-page tesseract fallback stays exactly as it is.
- Notes. `split_lossless` already chunks them correctly (0.7.120).

## Design

### 1. Blocks: one intermediate structure for every extractor

Extractors return an ordered `list[Block]` instead of a flat string
(`mcpbrain/sync/blocks.py`):

- `Heading(level: int, text: str)`
- `Paragraph(text: str)` — internal line breaks preserved
- `TableBlock(rows: list[list[str]], caption: str = "")`

Plus a `PartialBlocks` marker mirroring today's `PartialText`, so
`is_partial` keeps working.

Per format:

- **PDF** — `page.get_text("dict", sort=True)`. Each text block → `Paragraph`
  (its lines joined with `\n`). A line whose dominant span size is materially
  above the page's body size (median span size) → `Heading`, level by size
  rank. `page.find_tables()` regions → `TableBlock`, and text blocks whose bbox
  falls inside a table region are dropped so table text is not emitted twice.
  Pages below `_OCR_MIN_PAGE_CHARS` still go to `_ocr_page`, whose output
  becomes `Paragraph`s split on blank lines.
- **DOCX** — walk `document.element.body` in order, dispatching `w:p` and
  `w:tbl`, so tables sit where they appear. `Heading N`/`Title` styles →
  `Heading`. Table cells de-duplicated by underlying `_tc` identity (merged
  cells once), empty cells kept as `""` so columns align. Section headers and
  footers emitted once each (deduplicated across sections), at the start/end.
  Text boxes (`w:txbxContent`) emitted as `Paragraph`s at their anchor.
- **PPTX** — each slide → `Heading(2, "Slide N[: title]")`; shapes walked
  recursively through groups; tables → `TableBlock`; speaker notes
  (`slide.notes_slide.notes_text_frame`) → `Paragraph` prefixed `Notes:`.
- **Google Docs / Slides** — `_EXPORT` changes to the DOCX/PPTX MIME types and
  the result is run through the DOCX/PPTX extractors: one code path, not a
  Markdown parser. Drive caps exports at 10 MB; on an export-size error fall
  back to `text/markdown`, then `text/plain`, so no file that ingests today
  stops ingesting.
- **RTF** — a small in-tree decoder: groups, control words, `\par`/`\line`,
  `\'hh` (codepage), `\uN` with its skip count, and the ignorable
  destinations (`\*`, `fonttbl`, `colortbl`, `stylesheet`, `info`, `pict`).
  No new dependency.
- **Gmail bodies, calendar, anarlog** — unchanged extraction; they benefit from
  the chunker change in §2 only.

### 2. Rendering and chunking

`blocks.render(blocks, max_chars)` produces chunks:

- Blocks are packed greedily into chunks up to the budget, separated by `\n\n`.
- A `Heading` starts a new chunk when the current one is more than half full,
  so sections are not split across a chunk seam when avoidable.
- A `TableBlock` in a prose document is rendered with the SAME schema-enriched
  row sentences spreadsheets use (`tabular._row_sentence` /
  `_fit_row_sentence`), captioned with the heading trail it sits under,
  and grouped so one table's rows stay together where they fit.
- Every chunk carries `heading_trail` in metadata (e.g. `"Budget › Capital
  works"`). `embed.contextual_prefix` appends it to the embedding and BM25
  text exactly as `folder_path` is appended today, and `patch_chunk_metadata`
  already resets `fts_context_version` when such a field changes.
- Every chunk records its **source spans**: the source text of each block
  piece it contains, excluding renderer-synthesised text (captions, row-sentence
  column labels). §3 uses this to prove coverage.

`chunk_text` (used by Gmail, calendar, anarlog and the Paragraph splitter) gains
a four-level fallback: paragraph (`\n\n`) → **line** (`\n`) → sentence
(`(?<=[.!?])\s+`) → word. Line breaks inside a piece are never collapsed. The
50-word overlap applies only when a split lands inside a paragraph.

**Invariant (tested as a property):** when no paragraph exceeds the budget, the
new `chunk_text` returns byte-identical output to the current one. Hence an
owner with `chunk_total == 1` was never word-split and is provably unaffected.

**Versioning.** `CHUNKER_VERSION` is NOT bumped — it gates the table pipeline and
`bin/repair.py`'s selector, and bumping it would mark every spreadsheet stale.
Instead, chunks are stamped with:

- `extraction_version` — a per-MIME integer from `EXTRACTION_VERSIONS` in
  `sync/blocks.py` (PDF, DOCX, PPTX, RTF, Google Docs, Google Slides start at 1;
  absent = 0).
- `split_version` — `chunk_text`'s version (`SPLIT_VERSION = 1`; absent = 0).

`org_contracts.pipeline_fingerprint` (fed by `ingest_cache`) includes the file's MIME extraction version,
so shared-drive artifacts built by the old extractors stop matching and are
republished.

### 3. Content-preserving reflow (the carry-over)

A re-fetched owner is a **reflow** only when its source is proven unchanged:
Drive `modifiedTime` equals the stored `modified`; Gmail messages and anarlog
sessions are immutable per id (anarlog's note-shrink path already handles its
one mutable case). Anything else is an ordinary content change and takes the
existing path: delete, `invalidate_local_relations_for_docs`, re-enrich.

`reflow.carry_over(store, owner, old_chunks, new_chunks)`:

1. **Stitch** the old chunks (ordered by `chunk_index`) into one normalised text
   `O` — whitespace collapsed to single spaces — removing each seam's overlap
   by the longest suffix/prefix match (bounded by the 50-word overlap), and keep
   an offset → old doc_id map.
2. **Coverage.** A new prose chunk is *covered* when every one of its source
   spans, normalised, occurs in `O`, AND every old chunk overlapping those
   offsets was enriched. A new table chunk is covered when each of its non-empty
   cell values occurs in `O`. Covered chunks inherit `enriched`,
   `enriched_version`, `enrich_state`, `salience`, `memory_tier` and
   `memory_type` (majority over the overlapped old chunks). Uncovered chunks —
   genuinely new text such as speaker notes, text boxes, headers, PDF tables that
   were previously lost — get `enriched=0` and flow through normal enrichment.
3. **Remap.** Each old doc_id maps to the new chunk containing its start offset
   in `O`; if that text no longer exists (e.g. a repeated header now emitted
   once), to the positionally nearest new chunk, with `reason='nearest'`. The
   map is applied to `entity_relations.source_doc_id`,
   `entity_observations.source`, `actions.source_doc_id`,
   `actions.waiting_on_cleared_by_doc_id`, `graph_actions_legacy` /
   `graph_decisions_legacy.source_doc_id`, `recall_feedback.doc_id`
   (repointed) and `chunk_quality` (merged: exposures and uses summed,
   `memory_strength` max, `last_accessed` max).
   **doc_ids are positional and reused** (`gdrive-<fid>-<i>`,
   `gmail-<msg>-body-<i>`, …): new chunk *j* is written at the same id family,
   so the remap is a simultaneous in-place substitution over the owner's own id
   space (`i → j`), applied through a temp mapping table in one statement per
   target table, never a chain of single-row UPDATEs (which would re-map an
   already-remapped id).
4. **Log the map** in a new permanent table
   `reflow_map(id INTEGER PRIMARY KEY, owner TEXT NOT NULL,
   old_doc_id TEXT NOT NULL, new_doc_id TEXT NOT NULL, reason TEXT NOT NULL,
   at TEXT NOT NULL)` — an append-only log, not a lookup keyed on old id, since
   the same id is both an old and a new chunk. It is the audit trail and the
   input to `remap-gold`. For ids that no longer exist at all (the new
   document has fewer chunks than the old), `Store.read_doc`, drain's doc_id
   resolution and `org_contrib._chunk_provenance` fall back to the latest
   `reflow_map` row for that id, so a stale reference resolves rather than
   silently missing.

**Ordering.** Embeddings for the new chunks are computed BEFORE the write lock
(local compute), so recall never has a gap. Then, in one `BEGIN IMMEDIATE`:
write each new chunk over its positional id with its vector and FTS row
(`Store._write_cached_chunk_row`, the same helper the ingest-cache import uses)
→ carry state → apply the remap → append `reflow_map` → delete the owner's
old ids beyond the new chunk count (with their `vec_chunks` / `fts_chunks`
rows). Any exception rolls back to the untouched old chunks and the queue item
retries with backoff.

**Guards.**

- An owner with a pending or claimed enrichment unit is skipped this cycle (the
  item is re-queued with a short delay).
- After each commit, an **orphan check** for that owner: no row in any table in
  step 3 may reference a doc_id of that owner that has no `chunks` row. A non-zero count fails the item loudly (logged, recorded in
  `last_error`) and **halts the reflow cadence**; `doctor` reports the halt and
  an attended `bin/reflow.py resume` clears it after investigation — a wrong
  remap must stop, not propagate.

### 4. The reflow queue

- Items live in the existing `sync_queue` as `reflow:drive`,
  `reflow:shared_drive:<id>`, `reflow:gmail`, `reflow:anarlog`, drained by
  `work_queue` through a single `reflow` handler — retry, backoff and
  never-drop semantics come free.
- Reflow rows carry an epoch `modified_at`, so `due_sync_items`' newest-first
  order always serves real sync work first, and the reflow handler has its own
  per-cycle cap (at most 15 s of `CYCLE_BUDGET_S` and at most 10 items) so it can never
  starve sync.
- A `reflow_seed` cadence runs a **level-triggered selector** and tops the
  queue up to a window of 200 items (the enrichment-window pattern, not a
  7,000-row insert):
  - Drive files and Gmail attachments whose MIME's `extraction_version` is
    below `EXTRACTION_VERSIONS[mime]`;
  - Gmail bodies, calendar events and anarlog sessions with `chunk_total > 1`
    and `split_version < SPLIT_VERSION`.
  A reflowed owner stops matching, so the selector converges and the cadence
  goes idle.
- The handler: re-fetch → prove unchanged → extract to blocks → render →
  `carry_over`, or hand off to the ordinary change path.
- **Shared-drive ingest cache.** When `ingest_cache.try_import` brings in a
  newer-fingerprint artifact for a file whose content hash this install already
  holds, it goes through `carry_over` too, so other installs keep their
  enrichment.

**Safety gates.** The seed cadence starts only when the store has a backup that
succeeded within the last 24 hours. `PRAGMA integrity_check` runs when the
backlog reaches zero. `reflow_enabled` is a fleet-flippable kill switch
(`config.fleet_flag`), default ON; the new extractors apply to fresh ingests
regardless of it.

**Visibility.** `/api/status` and `doctor` report reflow progress (n of N
owners, carried-over vs re-enriched chunk counts, failures, halted state); the
dashboard shows the same.

### 5. The gold set

Gold cases name chunk doc_ids (`gdrive-<file>-0`), which a reflow re-points.
`bin/tenant.py` gains a `remap-gold` step that rewrites each case's
`expected_chunk_ids` through `reflow_map`, writing into the tenant repo (the gold
set is tenant data and never lives here). The gate is measured only after the
backlog completes — a gold number taken on a half-reflowed store measures
nothing (the 2026-09-10 lesson).

## Error handling

- Extractor failure on a file: the existing per-format `log.warning` + empty or
  `PartialBlocks` result, unchanged in spirit. A partial extraction is never
  used for a reflow (old chunks are kept; the item fails and retries).
- Google export-size error: fall back DOCX → Markdown → plain text.
- Source changed mid-reflow (modifiedTime moved): ordinary change path.
- Source gone (404/403 permanent): the existing deletion path for that source;
  the reflow item completes.
- Carry-over exception: full rollback, retry with backoff.
- Orphan check non-zero: item fails, cadence halts, `doctor` reports it.

## Testing

- **Extractor fixtures, generated in-test** with python-docx, python-pptx and
  fitz (no real documents): two-column PDF (reading order), PDF with a ruled
  table, heading hierarchy, DOCX with merged cells / a mid-document table / a
  text box / header and footer, PPTX with notes and a grouped shape, RTF with
  `\'hh`, `\uN` and an ignorable destination.
- **Property test**: `chunk_text` byte-identical to the current implementation
  whenever no paragraph exceeds the budget (the old implementation is kept as a
  test oracle).
- **Carry-over against a real `Store`, not fakes** — fakes are how this repo has
  shipped silently inert functions three times. Overlap stitching, full and
  partial coverage, table coverage, every remap target table, `reflow_map`
  fallback in `read_doc` / drain / org provenance, rollback on an injected
  failure, and the orphan guard halting the cadence.
- **Queue**: reflow never runs ahead of real sync items; per-cycle cap
  respected; selector converges to empty; backup gate blocks seeding.
- **Dry run on a copy of the live store** before release: reflow ~200 real
  owners, then assert orphan count 0, `integrity_check` ok, report the
  carried-over vs re-enriched ratio, and run the gold harness on the copy.

## Rollout

1. Implement and pass the above; full suite and ruff (Josh runs the full suite).
2. Dry run on a store copy (above).
3. Release per `docs/RELEASE-RUNBOOK.md` (four version files + `uv.lock`).
4. On this machine: install from the published index, confirm the running
   process's version, watch the reflow drain via `/api/status`.
5. When the backlog is zero: `integrity_check`, `remap-gold`, gold gate.

Estimated backlog on the author's store: ~7,000 owners, hours to a day of
background cycles; OCR-heavy PDFs dominate because scanned pages are re-OCR'd.
