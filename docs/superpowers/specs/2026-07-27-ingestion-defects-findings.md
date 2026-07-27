# Ingestion defects — verified findings (2026-07-27)

Input document for two follow-on specs (ingestion correctness, then repair
backfill). Every claim below was verified against the live store
(179,690 chunks) or the source, not inferred. Cited `file:line` refers to
`mcpbrain/` at commit `6386d3e`.

This is a findings register, **not** a design. No fix is proposed here beyond
noting what the defect is.

## How this was found

Investigating why the daemon's maintenance passes had not run since 2026-07-23
surfaced a wedged `run_one()`. Explaining a chunk-count jump from ~106k to
179,690 in a day then exposed the extraction defects, and a systematic audit of
every ingestion path found the rest.

---

## A. Content that never arrives

### A1. Email attachments are never ingested — CRITICAL
`_find_part_text` (`sync/normalise.py:57-66`) only returns `text/plain` or
`text/html` body parts. There is no attachment-handling code anywhere in the
repo (grep for `attachment`/`attachmentId` over the Gmail path: 0 matches).

A PDF emailed to the user is invisible to the brain, while the byte-identical
file in Drive is extracted normally. Likely the single largest content gap.

### A2. Several file types are silently dropped
`_MIME_EXTRACTION_META` (`sync/drive.py:58-71`) advertises types that the
fetcher never handles — `presentationml.presentation` (.pptx), `application/csv`,
`text/tab-separated-values` are in none of `_EXPORT`, `_DOWNLOAD_TEXT`,
`_DOWNLOAD_BINARY`, so `_fetch_text` returns `None` (`drive.py:112`) and the file
is dropped with no chunk, no stub and no log line.

Verified on the live store: **0 chunks for `.pptx`** (28 exist for native Google
Slides). Also dropped entirely: `.doc`, `.ppt`, `.pages`, `.numbers`, `.eml`,
`.zip`, all images, `application/rtf`, `application/json`.

The `processed` counters never see these files, so the dashboard reports a clean
sync while content is being discarded.

### A3. Bottom-posted replies are discarded
`strip_reply_chains` (`sync/normalise.py:84-92`) keeps only `text[:earliest]`.
Quoted history is correctly stripped rather than re-ingested, but a reply
written *below* the quote is silently thrown away with the quote.

### A4. Bulk/list mail dropped but counted as processed
`_is_bulk_or_auto` (`sync/normalise.py:131-146`) returns `[]` for anything
carrying `List-Id`, `List-Unsubscribe` or `Precedence: bulk` — which includes
most vendor and ministry-platform mail. `sync_gmail` still counts it as
processed (`sync/gmail.py:87`), so the drop is invisible.

### A5. Scanned PDFs can vanish without a trace
`is_scanned_pdf` (`sync/extractors.py:30`) is defined but never called; the real
gate is a char-count heuristic (`extractors.py:70`). Per-page OCR
failure/timeout (120 s, `extractors.py:108`) returns `""` and falls back to
`page_text` (`:80`), so a timed-out page yields nothing, unlogged. With
tesseract absent from PATH the whole scanned PDF returns `""` → no chunks, no
warning (`extractors.py:72-73`).

---

## B. Content that arrives degraded

### B1. Spreadsheet extraction keeps the noise and drops the signal — CRITICAL
`extract_text_from_xlsx` (`sync/extractors.py:205-231`) caps at **200 rows per
sheet** but does not bound **width**, and appends entirely-empty rows verbatim.

Live-store consequences:
- **66,653 chunks (37% of the corpus) contain zero alphanumeric characters** —
  ~2,000-char strings of `| | | | |` from empty spreadsheet cells. All are
  embedded.
- One file, `Fixed Assett Register 2023 onwards.xlsx`, produced **17,281
  chunks** (each ~2,000 chars, 300–500 pipes).
- **338 distinct files hit the 200-row cap**, including
  `Harvestnet _ 2026 Budget_.xlsx`, `Harvestnet _ 2025 Budget_.xlsx`,
  `Capes Church Budget - Spark.xlsx`,
  `THRIVE_FAMILY_CHURCH_INC_-_General_Ledger_Detail.xlsx`, and several risk
  assessments. Everything past row 200 per sheet is lost.

So the same files simultaneously bloat the store with empty cells and discard
their actual budget/ledger lines.

### B2. CSV and Google Sheets are split mid-row with no header
`drive.py:41` exports `application/vnd.google-apps.spreadsheet` as `text/csv`;
`_DOWNLOAD_TEXT` (`drive.py:45`) passes CSV through verbatim. `chunk_text`
(`chunking.py:154-179`) splits on `\n\n`; a CSV has no blank lines, so the whole
sheet is one "paragraph" and falls to the word-split branch
(`chunking.py:165-174`), cutting at 2,000 chars on whitespace — mid-row,
mid-cell. The header row survives only in chunk 0.

Verified: a mid-table chunk of the 2026 Budget reads
`,,Internet & IT support,"7,000",583,,"7,000",583,,,0,,,0,` — headerless
numbers that neither the embedding nor a reading model can interpret.

### B3. 15,576 chunks exceed the embedder's window
Chunks over 2,000 chars (14,318 of them Drive) are silently truncated by the
512-token BGE model at embed time, so their tails are unsearchable. Not logged,
not counted.

### B4. Thread expansion emits scrambled paragraph order
`store.thread_chunks` documents "Order is not guaranteed" (`store.py:2364`).
`retrieval_expand._by_date` (`retrieval_expand.py:37-38`) sorts by date only,
and every chunk of one message shares a date, so a stable sort preserves raw
SQLite scan order before `expand_parent` joins them (`:58-59`). Any email over
2,000 chars is injected with its paragraphs out of order. (`chunks_for_file`
does sort by index, `store.py:1526`, so only the thread path is affected.)

### B5. Stale chunks are orphaned when a document shrinks
Drive writes `gdrive-<fid>-<i>` for `i in 0..n-1` (`drive.py:158-165`) and both
`_cache_first_extract_one` (`:262-263`) and `sync_drive` (`:334-335`) only
upsert. Nothing deletes indices `n..m` left by a previous, longer version
(`store.doc_ids_for_file` is called only on file removal, `drive.py:446`).
Deleted paragraphs stay searchable indefinitely and are re-fed to expansion as
current content.

### B6. `chunk_text` can emit empty and oversize chunks
`chunking.py:172-174`: when a single whitespace-delimited token exceeds
`max_chars`, the first iteration appends `current` while still `""`, writing a
zero-length chunk; the following `current` then exceeds `max_chars`. Verified:
6 zero-length and 36 sub-5-char chunks exist.

### B7. Eight silent `except Exception: return ""` in extraction
`extractors.py:39, 47, 64, 82, 99, 111, 173, 229`, plus `strip_html`
(`normalise.py:79`). A corrupt DOCX or XLSX becomes `""` → `normalise_drive`
returns `[]` (`drive.py:129-130`) → indistinguishable from "unsupported type".
Never retried differently, never surfaced.

### B8. Enrichment can present a partial document as whole
`group_unenriched_threads` (`thread_enrich.py:98`) iterates
`store.unenriched_chunks()` and `reassemble_thread` (`:148`) joins only those.
If part of a thread was already enriched — or cold-marked, excluded at
`store.py:1264` — the model receives a partial document with no gap marker.

---

## C. Provenance and metadata gaps

### C1. No chunk records how many chunks its document has
154,601 chunks carry `chunk_index`; **zero** carry a total
(`drive.py:159`, `normalise.py:179` stamp only the index). Given "chunk 7",
nothing can tell whether the document has 8 chunks or 17,281 — so there is no
integrity check for partial ingestion, and no consumer can detect the B5
orphaning.

### C2. Enriched chunks are date-blind — 21,162 chunks
Every `gmail_enriched_v2` chunk carries only `source_type`, `thread_id`,
`subject`, `org`, `content_type`. No date in any form
(`date`/`date_iso`/`start`/`modified`/`modifiedTime`), so
`importance.recency_decay` returns the neutral `0.5` fallback
(`importance.py:165-168`) for all of them. These are the LLM-digested summaries
— the highest-value chunks in the store — and the only significant population
the recency axis cannot rank. Their source chunks do carry dates; the value is
simply not propagated at write time.

For scale: 155,838 of 179,778 chunks (87%) *do* have a usable date. The gap is
specifically the enriched layer plus 2,778 note chunks.

### C3. Enriched chunks lose the message-level link
They retain `thread_id` but carry no `message_id`, so a fact can be traced to a
thread but not to the message it came from. (Raw Gmail chunks carry both —
`normalise.py:167-168` — so the base linkage is sound.)

### C4. Calendar-derived chunks are mislabelled as Gmail
Observed: a chunk with `source_type: gmail_enriched_v2` whose `thread_id` was
`cal-e734d9f93c894a5a81e3230300748014`.

### C5. `folder_path` is read but never written
`embed.contextual_prefix` (`embed.py:55`) reads `metadata["folder_path"]`;
`normalise_drive` never sets it. Every Drive contextual prefix therefore lacks
folder context — dead provenance in a default-ON retrieval feature.

### C6. Recipient lists truncated
Gmail `to[:300]` / `cc[:300]` (`normalise.py:171-172`) — an all-staff email
loses most of its recipients from metadata.

### C7. Most of the Drive corpus was ingested by the old extractor
9,353 files / 63,237 chunks have no `extraction_method`, predating the per-type
structural extraction work: **3,437 PDFs, 2,714 docx, 1,849 xlsx, 871 Google
Docs, 379 Google Sheets**. Compare the current path: 720 PDFs, 336 docx, 684
spreadsheets. The majority of Drive documents are therefore lower-fidelity than
the code is now capable of producing. (`.xlsx` remains correctly gated by mime
via `_COLD_DRIVE_MIMES`, `prepare.py:235-242`, so this is a quality issue, not a
cost one.)

---

## D. Duplication

96,335 redundant copies exist (54% of the store): 179,690 chunks against 83,355
distinct `content_hash` values. Dominated by B1's empty-pipe chunks (65,770
copies of a single hash), but also includes genuine duplicate files — the asset
register exists three times in Drive (two identical names plus a `(1)` copy),
each chunked independently.

## E. Calendar chunking — noted, low impact

`normalise_calendar` (`sync/calendar.py:70`) emits exactly one chunk per event
with the description inlined (`:57`), never calling `chunk_text`. A long agenda
is therefore truncated by the embedder rather than split. Measured impact is
small: of 1,149 calendar chunks, max length 2,977 and only 4 exceed 2,000 chars.

---

## Suggested spec split

- **Spec 2 — ingestion correctness.** A (coverage), B1–B7 (extraction and
  chunking fidelity), C (provenance). Must land before any repair, or a
  backfill re-imports the same defects.
- **Spec 3 — repair backfill.** Re-extract the 338 truncated sheets and the
  9,353 legacy files; purge empty and duplicate chunks; must clear the matching
  vector and FTS rows, and be gold-gated (abort on recall@10 / MRR regression),
  following the attended, backup-gated `bin/consolidate.py` precedent.
