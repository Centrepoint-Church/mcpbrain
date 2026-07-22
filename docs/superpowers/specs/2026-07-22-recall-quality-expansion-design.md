# Recall quality: small-to-big expansion + cross-encoder rerank + contextual indexing

**Date:** 2026-07-22
**Status:** design approved, pending spec review

## Motivation

`brain_search` (and the UserPromptSubmit auto-RAG injection) recall via
`daemon.search` → `hybrid_search`: vector KNN + keyword FTS fused with RRF,
returning the top-k **individual chunks** as isolated fragments. Two known gaps:

1. **No context stitching.** A hit on chunk 764 of a 2,303-chunk PDF returns that
   fragment alone — not the surrounding pages, nor a grouped view of the file.
   Chunks carry `file_id`/`thread_id`/`chunk_index` (the parent pointer) but
   retrieval never uses it.
2. **Context-blind *keyword* scoring.** Contextual *embeddings* already exist and
   ship ON (`index_pending` prepends `embed.contextual_prefix` behind the
   `contextual_retrieval` flag — gold-validated +0.10 recall@10 / +0.175 MRR,
   2026-06-24). But `write_embedding` indexes **FTS from raw `text`**, so the
   keyword/BM25 arm is still context-blind — a page that never repeats its
   document title won't match a title-ish keyword query.

Web research (LangChain parent-document, LlamaIndex sentence-window /
auto-merging, Anthropic Contextual Retrieval, Elastic RRF/MMR, Pinecone
rerankers, Liu et al. lost-in-the-middle, RAPTOR, ARAGOG, RAGAS) converges on
the **"small-to-big"** pattern and one hard ordering rule: **rank on small
chunks; expand last, right before the model.** Full research synthesis captured
in the brainstorm; key sources cited inline below.

## Scope

Three synergistic, independently-measurable levers, built as sequential phases in
one project, **each gold-gated before the next**. Every new stage is a
`config *_enabled()` flag defaulting **OFF** — shipping the code does NOT flip the
flag; each flag is turned on only after live-store gold-gate validation.

Out of scope (flagged for later): LLM-generated contextual blurbs (Anthropic
Contextual Retrieval's expensive variant); **late chunking** (needs a long-context
embedding model swap, not bge-small); a graph↔embedding cross-over.

## Pipeline (target shape)

```
vec_knn + fts  (wide, N≈60)
  → RRF (k=60)                     [existing]
  → 3-axis boost                   [existing, flag-gated]
  → cross-encoder rerank → top-M   [PHASE B]
  → per-parent cap / MMR           [PHASE A]
  → sufficiency gate               [existing]
  → EXPAND survivors               [PHASE A]  ← the only "big" step, last
  → head-and-tail order            [PHASE A]
```

Ranking/filtering all happen on small chunks; expansion is the final step so it
can't blunt the reranker or trigger lost-in-the-middle (Liu et al., 2307.03172).

## Phase A — read-side expansion

New pure module `mcpbrain/retrieval_expand.py`, invoked at the end of
`daemon.search` (after ranking + sufficiency gate). Uses existing store
primitives: `thread_chunks(thread_id)`, `chunks_for_file(file_id)`,
`doc_ids_for_file(file_id)`.

Per-survivor expansion policy (keyed on chunk metadata):
- **`thread_id` (email thread)** → return the whole thread (stuff). Threads are
  small and coherent; research says stuffing is correct here.
- **short file** (≤ **15** chunks) → return the whole parent document.
- **large file** → **contiguous span-stitch** by `chunk_index`: matched chunk ±N
  (**N = 3** default), never the whole file (a 2,303-chunk book returned whole =
  token blowup + lost-in-the-middle; explicit anti-pattern).

Cross-cutting:
- **max-parents cap** (default top **5** distinct files/threads) and collapse
  multiple same-parent hits into one grouped result, so one big document can't
  crowd out others (MMR λ≈0.7 or a max-per-`file_id` cap — MMR optional).
- **Token budget**: total expanded context bounded, hooked to the existing MCP
  response-trim budget; overflow drops lowest-ranked parents first (logged, no
  silent truncation).
- **Head-and-tail ordering**: highest-ranked passages first and last.
- Overlap/near-duplicate spans deduped.

Flag: `retrieval_expand` (default OFF). Params (`expand_window_n`,
`expand_short_doc_max_chunks`, `expand_max_parents`, `expand_token_budget`) in
config, defaults above, tunable via the eval sweep.

## Phase B — cross-encoder rerank

In `query_router.route()`, alongside the existing lexical `_token_overlap_rerank`.
Widen the fused candidate set (~60) → cross-encoder rerank → top-10, **on the
small chunks** (before Phase A expansion).

- **Backend from the start:** fastembed `TextCrossEncoder` with
  `Xenova/ms-marco-MiniLM-L-6-v2` (~80 MB). No new native dep — reuses the
  onnxruntime the daemon already ships; runs daemon-side (recall's home), so the
  native-dep-free MCP client is untouched. **Lazy-downloaded** like bge-small
  (wizard/`/api/model` pattern); recall degrades to the pre-rerank order if the
  model isn't present yet.
- The existing pure-Python lexical reranker stays as a **fallback backend**.
- Config: `retrieval_rerank` (enable, default OFF) + `rerank_model` (default
  `ms-marco-MiniLM-L-6-v2`; `"lexical"` selects the no-model fallback;
  `bge-reranker-base` available for a quality/size trade).

## Phase C — contextual BM25 (complete the existing contextual-retrieval feature)

**Already built (do not re-implement):** contextual *embeddings* + the
embed/display split. `index_pending` and `store.embed_and_write` embed
`embed.contextual_prefix(metadata) + text` while the `text` column (returned to
callers) stays raw. Gated by the existing `contextual_retrieval` flag (default
ON), gold-validated +0.10 recall@10 / +0.175 MRR (2026-06-24). `contextual_prefix`
already handles gmail/gdrive/calendar (sender, subject, `file_name`,
`folder_path`, `org`, dates) and is correctly passage-only.

**The gap:** `store.write_embedding` (and the cached-chunk write path) populate
`fts_chunks` from **raw `text`**, so the keyword arm never sees the prefix.

Phase C = **contextualize the FTS arm too**, under the *same* `contextual_retrieval`
flag (no new flag — this completes the existing feature):
- In the FTS-write sites (`write_embedding`, `_write_cached_chunk_row`), index
  `contextual_prefix(metadata) + text` into `fts_chunks` when
  `contextual_retrieval` is on, instead of raw `text`. The `text` column and all
  returned text stay raw (unchanged).
- **Backfill: FTS re-index only — NO re-embed** (embeddings are already
  contextual). Re-run the FTS insert for embedded chunks in bounded batches
  (resumable), so existing rows pick up the contextual BM25 text.

This is much smaller than a full contextual-indexing build: no embed/display
split to add (exists), no re-embed backfill, no new flag.

## Evaluation (per phase — gate before proceeding)

- **Gold gate** (`tests/eval/run_eval.py --gold --k 10`, production path) after
  each phase: recall@10 ≥ 0.55 and MRR ≥ 0.35 (hold the current 0.750 / 0.514).
- **Bloat guard**: assert expansion stays within the token budget and does not
  regress a context-precision proxy — a technique can win recall while flooding
  context (ARAGOG, RAGAS context-precision).
- Sweep `expand_window_n`, short-doc threshold, top-k, rerank model empirically;
  the auto-merging literature shows the right config is corpus-specific.
- Flip each flag ON only after live-store validation.

## Module boundaries / isolation

- **A**: `retrieval_expand.py` — pure functions over (ranked hits, store);
  testable on fixture chunks with no daemon.
- **B**: a `rerank` backend in `query_router` (lexical | cross-encoder), model
  access behind the embedder's lazy-load pattern.
- **C**: an `embed_text` derivation helper (pure) + ingest/embed-path wiring + a
  version-bump backfill.

Each phase is independently flag-gated and reversible (A/B are read-only; C's
backfill only changes embedded/indexed text, never the returned text, and is
re-runnable).

## Risks

- **Context bloat / lost-in-the-middle** — mitigated by the token budget,
  max-parents cap, span-stitch (never whole-file), head-tail ordering.
- **Rerank latency** — MiniLM-L-6 on CPU ~100–300 ms on a ~60-candidate set;
  acceptable for recall; measured, and the stage is skippable via flag.
- **Backfill cost (C)** — bounded per-cycle via the reflow cap; no LLM.
- **Eval attribution** — phases gated separately so each lever's effect is
  measurable.
