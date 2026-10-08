# Table flow + document collapse — design

**Date:** 2026-10-07 · **Status:** proposed · **Follows:**
`2026-09-24-extraction-fidelity-design.md` (whose binding gold gate this fixes)

## 1. Problem

The extraction-fidelity reflow failed its binding gold gate. On the same harness,
same day, the pre-drain snapshot scores MRR 0.548 and the live store 0.464 (main
20-case set; recall@10 unchanged at 0.850). The floor is MRR ≥ 0.546.

**Cause (verified, 2026-10-06).** The new block extractors keep tables in true
document order; the old DOCX extractor appended every table at the end. In
`blocks.render` a table becomes near-full-size pieces, so when one arrives the open
prose chunk flushes early (947 chars in the worst case) and the prose AFTER the
table starts a fresh chunk. Prose that belongs together — one board meeting's
agenda items either side of an action-list table — ends up in separate chunks,
and no single chunk matches a query about both. Word/Google-Doc chunks shrank
(median 1796 → 1559, p25 1732 → 1105). Headings are not the cause: DOCX headings
come only from real heading styles.

**Measured fix (throwaway spike, 2026-10-07; 344-file fair competition set — every
block-format file in the top 30 of every gold query plus every expected document —
re-fetched and re-rendered on scratch copies; V0 control reproduced live exactly):**

| Variant | main MRR / R@10 | ops MRR / R@10 |
|---|---|---|
| live / V0 control | 0.464 / 0.85 | 0.283 / 0.40 |
| V1 prose flows across tables | 0.529 / 0.90 | 0.308 / 0.35 |
| **V1 + document collapse (MaxP)** | **0.543 / 0.90** | **0.314 / 0.40** |
| V2 table rows fill the open chunk | 0.465 / 0.85 | 0.283 / 0.40 |
| collapse alone on today's chunks | 0.506 / 0.85 | 0.283 / 0.40 |

Score aggregation that ADDS evidence across chunks (SumP, best + capped bonus) lost
on both sets and both stores: long threads and many-chunk documents crowd out the
precise answer. It is out of scope and must not be reintroduced without new
evidence. Spike artefacts: `/private/tmp/claude-501/docagg/` (not committed).

## 2. Goals / non-goals

**Goals:** restore and exceed the pre-drain ranking without giving up true
document order; make one document unable to occupy several result slots; give the
gate enough cases to mean something.

**Non-goals:** changing the embedding model, LLM-generated chunk context, a
reranker (a cross-encoder lost on this corpus in 0.7.100), summed/bonus score
aggregation, moving tables back to the end of the document.

## 3. Design

### 3.1 Two-stream packing in `blocks.render`

`render` keeps building the same pieces (headings, paragraphs, `_table_pieces`) in
document order, but packs them into **two open chunks**:

- **Prose stream** — heading and paragraph pieces. Packs exactly as today
  (`size > max_chars` flushes; a heading flushes when the open chunk is past
  `max_chars // 2`). A table arriving does **not** flush it.
- **Table stream** — table pieces. Packs consecutive table pieces up to
  `max_chars`. A prose piece arriving does **not** flush it either, so small tables
  separated by a sentence of prose share one chunk instead of each becoming a tiny
  one (the spike's V1 flushed it on every prose piece and doubled the under-600
  share, 5.3% → 10.5%; this closes that).
- **Section boundary:** a heading piece flushes the open table chunk before it is
  added, so tables never pack across sections. The prose rule for headings is
  unchanged.
- **Order:** each emitted chunk is ordered by the document position of its FIRST
  piece. Output order is therefore still document order by chunk start.
- **heading_trail** of a chunk is its first piece's trail (unchanged rule).
- **Spans** stay source-verbatim; each piece carries its own spans exactly as
  today. Reflow coverage proofs are unaffected.
- Everything else in `render` (oversize paragraph splitting, labels, lost-cell
  pieces, `has_content` filter) is unchanged.

`to_text`/`from_text` and every non-block MIME (`chunk_text` prose) are untouched.

### 3.2 Document collapse in `hybrid_search`

New keyword `collapse_documents: bool = False` on `hybrid_search`. When True:

- **Retrieval depth.** Each ranker fetches `limit * 6` (the spike's measured depth:
  `hybrid_search(limit=30)` → 60 per ranker, top 10 documents). Without the wider
  pool a document's better chunk is often outside the candidate set.
- **Grouping key** (`_document_key(hit)`): a Drive chunk → `file:<file_id>`; an
  `enriched-<cluster>` digest → the cluster's key (`_cluster_key`), so a digest and
  its own raw chunks are one document; anything else (a Gmail message, calendar
  event, note) → `_doc_root(doc_id)`. **Never group Gmail by thread**: the spike
  measured that hiding a thread's other messages loses the expected message.
- **Score:** a document's score is its best chunk's score (MaxP) after the existing
  three-axis boost and dedupe passes; it is represented by that best chunk. No
  aggregation across chunks.
- The collapse runs after `_dedupe_by_cluster` and the content-hash dedupe, before
  truncation to `limit`, so a freed slot goes to a different document.
- Each returned hit gains `doc_hits: int` (how many of that document's chunks were
  in the pool) — cheap provenance for callers and for diagnosing.

**Where it is on.** A fleet flag `retrieval_collapse_documents` (`config.fleet_flag`,
default **True**, local kill switch honoured). `daemon.search` passes it through to
`hybrid_search` and to `query_router.route` (which forwards to its own
`hybrid_search` calls). `tests/eval/run_eval.py::production_search_kwargs` includes
it, so the gold gate measures what users get. `retrieval_expand` (injection) is
unaffected: it already works from parent documents.

### 3.3 Versioning and rollout

- `EXTRACTION_VERSIONS`: every block MIME **1 → 2** (render is shared by all six,
  and Gmail attachments go through the same render). The reflow selector, the
  ingest-cache `+x<N>` fingerprint, GC and bootstrap all follow the constant; no
  new machinery.
- **Reflow cost on this store:** 10,426 Drive documents, 67,446 chunks (64,339
  enriched), plus block-format Gmail attachments. Enrichment carries over through
  `apply_reflow` exactly as in the 0.7.132 rollout; only genuinely new text
  re-enters enrichment. Every install reflows these once.
- **Fleet:** shared-drive cache artifacts for block MIMEs go stale (`+x1` → `+x2`)
  and are re-extracted once per install; foreign `+x1` artifacts are skipped by the
  0.7.139 refused-delete handling.
- Collapse needs no migration and takes effect at install.

### 3.4 Gold set growth

Both gold files grow toward 40 cases each, adding cases whose answer spans two
parts of one document (agenda item + figure, decision + owner), across Word, Google
Docs, PDF and Gmail. Candidates are drafted from the live store (query + expected
chunk ids, verified to exist) and **reviewed by the owner before they count**. Gold
files are tenant data: they live only in `mcpbrain-tenant/eval/` and the gitignored
`tests/eval/`; never in the public repo.

## 4. Acceptance gates (in order)

1. **Pre-build spike re-run** of the final §3.1 packer (two streams, section
   flush) on the same 344-file competition-set harness: must be ≥ the spike's V1 +
   collapse (main 0.543 / 0.90, ops 0.314 / 0.40) and the under-600 share must be
   ≤ V0's 5.3% + 1 pt. If the table-stream change loses to V1, ship V1's table
   rule instead and record why.
2. **Unit tests** pin: prose continuity across a table; consecutive small tables
   sharing a chunk across intervening prose; no table chunk crossing a heading;
   chunk order by first piece; spans verbatim; byte-identical output for a
   document with no tables; collapse grouping (Drive file, digest with its file,
   Gmail by message not thread); `doc_hits`; collapse off = today's output exactly.
3. **Dry run** (`bin/reflow_dryrun.py`, store copy): 0 failures, no new orphans,
   integrity ok.
4. **Release gates** as the runbook: full suite under the fleet's resolved
   versions, ruff, tenant check, wheel contents.
5. **Binding gold at backlog 0** (bar changed by the owner, 2026-10-08): on the
   main set, recall@10 ≥ 0.850 AND MRR no worse than the pre-drain snapshot
   measured the same day on the same harness (0.548 on 2026-10-06) by more than one
   rank step; AND no regression on the ops set versus that same-day baseline;
   re-measured on the grown set once it is reviewed. The fixed 0.546 floor was
   retired: n=20 puts one rank step at 0.01-0.05, so a fixed floor 0.003 from the
   measured result would pass or fail on noise.
6. **Latency:** `brain_search` p95 on the live store within +25% of today
   (collapse fetches a 6× pool).

## 5. Risks

- **Small n.** 20 cases per set; one rank change moves MRR up to 0.05. Gate 5 adds
  the second set and the grown set so a single case cannot decide it.
- **Latency of the deeper pool** — gate 6; the depth multiplier is a constant and
  can be lowered with measured evidence.
- **Second reflow within weeks.** Proven machinery, same carry-over; the cost is
  Drive quota and one more backlog. No new schema.
- **Callers that relied on several chunks of one document** in `brain_search`: the
  best chunk plus `doc_hits` is returned; `brain_read` fetches the rest.
