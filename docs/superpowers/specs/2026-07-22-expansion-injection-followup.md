# Follow-up: injection-only expansion + an answer-quality metric

**Date:** 2026-07-22
**Status:** deferred follow-up (spec only, not implemented)
**Supersedes Phase A of:** `2026-07-22-recall-quality-expansion-design.md`

## Why this exists

Phase A (read-side small-to-big expansion) was built, reviewed clean, then
**reverted** after gate testing (commit `7808b77`). The gate showed recall@10
fell 0.750 → 0.300 with expansion on. That is **not** a defect — it is the
consequence of two things:

1. **Wrong metric.** Expansion deliberately returns *fewer, richer* results
   (10 hits → ~4.5 grouped/stitched parents under a token budget). recall@k over
   a candidate list therefore drops mechanically. recall@k measures *retrieval*;
   expansion improves *context quality for answer generation*, which recall@k
   cannot see.
2. **Wrong consumer.** Expansion was wired into `daemon.search`, which also feeds
   `brain_search`'s flat candidate list — so it degraded the list use-case. It
   belongs only on the **injection** consumer (UserPromptSubmit auto-RAG), where
   "a few rich, coherent contexts" is exactly what you want.

The reverted implementation is sound and lives in git history
(`retrieval_expand.py`, commits `bdb18c2`..`a3d6bcd`); the fix is consumer +
metric, not the expansion logic itself.

## What to build

### 1. An answer-quality eval (prerequisite — nothing about expansion can be judged without it)
- A gold set of query → ideal-answer (or query → must-contain-facts) cases,
  distinct from the retrieval gold set.
- RAGAS-style metrics computed over the *stitched context*: **context precision**
  (does the returned context rank relevant material first, without bloat),
  **context recall** (does it contain the facts needed to answer), and
  **faithfulness** of an answer generated from it.
- A harness (sibling to `tests/eval/run_eval.py`) that runs these against the
  injection path.

### 2. Injection-only expansion
- Apply expansion (the reverted `retrieval_expand.expand_hits`) **only** on the
  UserPromptSubmit auto-RAG path, not in `daemon.search`/`brain_search`.
- `brain_search`'s candidate list stays flat and unchanged → retrieval recall@k
  is unaffected by construction (re-confirm on the retrieval gold gate: still
  0.750/0.514).
- Behind a flag, default OFF.

### 3. Validation gates
- **Must hold:** retrieval gold gate unchanged (expansion doesn't touch
  `brain_search`).
- **Must improve:** the new answer-quality metric (context recall / faithfulness
  up, context precision not regressed by bloat) on the injection path.
- Only then flip the flag ON.

## Out of scope / decided
- **Phase B (cross-encoder rerank) is NOT revisited here.** The gate showed it is
  net-negative on this corpus (MRR 0.514 → 0.354 with a loaded, working model);
  it was dropped, not deferred. Revisit only with a domain-fit reranker AND
  scoring the contextual-prefixed text, if ever.
- **Phase C (contextual BM25) shipped** and is unaffected by this follow-up.
