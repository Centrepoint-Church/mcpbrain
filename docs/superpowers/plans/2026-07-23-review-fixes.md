# Review Fixes (2026-07-23) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. TDD each task; checkbox steps.

**Goal:** Fix the 7 findings + simplicity/consistency flags from the adversarial review of the recall-quality + fleet work.

**Scope:** four independent workstreams (sequential — some share files). Each behind its subsystem; no version bump here (release is a separate step). Gold gate must hold recall@10 0.750 / MRR 0.514; brain_search stays flat.

## Global Constraints
- Recall/prompt paths must never raise (keep fail-open try/excepts).
- `retrieval_expand` stays default OFF; contextual_retrieval default ON.
- Run scoped tests with `-n0`; human runs full suite. Commit per green step.

## Design decisions (resolved)
- **Fleet flag precedence (#3 + kill-switch):** coerce values to real bool (`True`/`False`, `"true"/"false"` case-insensitive, `1/0`; else default + warn). Precedence: an **explicit local `False` wins** (emergency kill-switch, matching the codebase norm) → else **org overlay** (fleet enable reaches everyone) → else local value → else default.
- **Transient-wipe (#4):** `read_org_config` returns `None` on FETCH/parse FAILURE, `{}` on genuinely-absent, dict on found. `merge_org_config`: `None` → keep the prior overlay, do NOT write; `{}`/dict → wholesale-write the allowlisted subset (so a real deletion still reverts).
- **Startup Drive I/O (#6):** `_maybe_merge_org_config` returns early unless `auth.token_path().exists()` (no creds → no Drive/auth attempt, no per-boot warning).
- **BM25 marker (#7):** writers stamp `fts_context_version = FTS_CONTEXT_VERSION` ONLY when the contextual prefix was actually applied (flag ON); a raw write (flag OFF) stamps `0`. So OFF→ON re-index self-corrects (raw rows stay v0 → re-picked), and fresh contextual writes aren't reprocessed. `_fts_text` returns `(text, applied: bool)`.
- **Expansion budget (#1/#5):** ONE budget, in **chars**, owned by `expand_hits` (`char_budget`), binding the first parent too (truncate it to fit). Select-within-budget FIRST, `_head_tail`-order LAST, so the consumer never front-truncates an ordered set. `prompt_recall` passes its budget as `char_budget` and does NOT re-truncate expanded items.
- **Drive over-mark (#2):** in `drain`, drop `cold` chunks from the resolved `doc_ids` before `apply`/`mark_enriched` — a Drive extraction never marks/provenances chunks its text didn't include.

---

### Task 1 — Expansion pipeline (fixes #1, #5, budget-unify, span gap, naming)

**Files:** `mcpbrain/retrieval_expand.py`, `mcpbrain/prompt_recall.py`, `mcpbrain/config.py` (expand_params), `mcpbrain/daemon.py` (search call). **Test:** `tests/test_retrieval_expand.py`, `tests/test_prompt_recall.py`.

Changes:
- `expand_hits(store, hits, *, window_n=3, short_doc_max_chunks=15, max_parents=5, char_budget=4000)`: rename the accumulator `expanded`→`results`. Build each selected parent's text (capping a single parent's text to `char_budget`), accumulate in rank order until `char_budget` (the FIRST parent is truncated to `char_budget` rather than admitted whole), THEN `_head_tail`-order the selected set. Drop the `//4` token pseudo-budget; one char budget.
- `expand_parent` large-file span-stitch: when joining non-contiguous index spans, insert a gap marker (e.g. `"\n\n[…]\n\n"`) between non-adjacent runs so disjoint text isn't presented as contiguous.
- `config.expand_params`: replace `token_budget=6000` with `char_budget=4000` (matches the injection budget); keep window/short/max_parents.
- `prompt_recall`: for the expanded path, pass `char_budget=_EXPANDED_MAX_TOTAL` to expansion (via the daemon? no — expansion runs daemon-side). Simpler: keep `_EXPANDED_MAX_TOTAL` as the daemon-side `char_budget` default and have `_format_context(expanded=True)` NOT re-truncate per-item/total (trust expand_hits' budget) — just join the already-budgeted stitched results (still respect `_KEEP` if desired, but do not drop the tail). Remove `_EXPANDED_SNIPPET` re-truncation for expanded items.

TDD: (a) a ≥3-parent case asserts the 2nd-best parent survives (regression test for the drop bug); (b) a huge first parent is truncated to `char_budget` (not returned whole); (c) span gap marker present for non-contiguous; (d) flat path unchanged.

### Task 2 — Enrichment marking precision (fix #2 + key-precedence helper)

**Files:** `mcpbrain/drain.py`, `mcpbrain/thread_enrich.py`. **Test:** `tests/test_drain.py`, `tests/test_thread_enrich.py`.

Changes:
- `drain`: after `doc_ids = store.doc_ids_for_messages(msg_ids)` (and the fallbacks), filter out cold chunks before `apply`/`mark_enriched`: `doc_ids = store.drop_cold(doc_ids)` (add a small `store.drop_cold(doc_ids)->list` helper, or inline a query `WHERE doc_id IN (...) AND COALESCE(enrich_state,'')!='cold'`). A Drive extraction (file-wide resolve) then marks only the hot chunks it actually covered — matching the message-precise email path.
- `thread_enrich`: extract one shared key-precedence helper used by BOTH `_group_key` and `reassemble_thread` (`thread_id → file_id → message_id → doc_id`) so they cannot drift. (reassemble currently checks file_id first; unify to the shared order — verify no behavior change for real data: Drive chunks have no thread_id, so file_id still wins.)

TDD: (a) a Drive file with mixed hot+cold chunks → drain marks only the hot ones, cold stay `enriched=0`; (b) the shared key helper returns the same key for both call sites across thread/file/message/doc cases.

### Task 3 — Contextual BM25 marker + flag consistency (fix #7, BM25-2/3/5)

**Files:** `mcpbrain/store.py`, `mcpbrain/embed.py`, `mcpbrain/index.py`. **Test:** `tests/test_contextual_bm25.py`, `tests/test_store.py`.

Changes:
- `_fts_text(text, metadata, *, home)` → returns `(fts_text, applied)`; take an explicit `home` (default `app_dir()`) so it reads the same config as the embed side.
- `write_embedding(rowid, vector, *, home=None)`, `_write_cached_chunk_row(..., home)`: stamp `fts_context_version = FTS_CONTEXT_VERSION if applied else 0` at write time.
- `embed_doc`: gate the passage prefix on `contextual_retrieval_enabled(home)` (match `index_pending`), and thread `home` through.
- `reindex_fts_batch`: unchanged selection (`version < CURRENT`), but now write-time stamping means it only ever migrates genuinely-old rows; a raw row written under an OFF flag stays v0 and is re-picked when the flag flips ON.
- `index.py`: log (once) if `reindex_fts_batch` raises, instead of silent `pass`.

TDD: (a) contextual write stamps CURRENT version, raw write (flag off) stamps 0; (b) reindex is a no-op on a store whose rows are all current (idempotent, 2nd pass = 0); (c) flag OFF→ON: a v0 raw row gets re-indexed to contextual; (d) embed_doc respects the flag.

### Task 4 — Fleet config robustness (fixes #3, #4, #6 + churn/redundancy)

**Files:** `mcpbrain/config.py`, `mcpbrain/fleet.py`, `mcpbrain/daemon.py`. **Test:** `tests/test_config_fleet_flag.py`, `tests/test_daemon_org_config.py`, `tests/test_fleet*` (org-config).

Changes:
- `config.fleet_flag`: implement the resolved precedence + a `_coerce_bool` helper (bool/`"true"/"false"`/`1/0`; else default + `log.warning`). Local explicit `False` wins; else org overlay; else local; else default.
- `fleet.read_org_config`: return `None` on fetch/parse failure (keep `{}` for absent). Distinguish `_find_file_id`→None (absent → `{}`) from any exception in list/get/parse (failure → `None`).
- `fleet.merge_org_config`: if `read_org_config` returns `None` → log + return the CURRENT overlay unchanged (no write). Else compute `allowed`; **skip the write when `allowed` equals the current overlay** (avoid churn / narrow the lost-update window); write only on change.
- `daemon._maybe_merge_org_config`: return early unless `auth.token_path().exists()`; drop the redundant folder re-derivation (let `merge_org_config` own folder resolution) — call it inside the try.

TDD: (a) `fleet_flag`: org `true` enables; local `false` overrides org `true` (kill-switch); `"false"` string coerces to False; garbage → default+warn; (b) `read_org_config` returns `None` on a raising drive_service, `{}` on absent; (c) `merge_org_config` keeps prior overlay on `None`, reverts on `{}`, no-writes when unchanged; (d) `_maybe_merge_org_config` skips when no token file, runs when present.

---

### Task 5 — Validation (controller): full suite + gold gate + injection on/off spot-check (tabular gap-marker + 2nd-best-survives).

## Self-Review
All 7 findings mapped: #1/#5→T1, #2→T2, #7(+BM25-2/3/5)→T3, #3/#4/#6→T4. Simplicity flags: budget-unify + metadata (T1), key-precedence helper (T2), embed_doc/_fts_text home (T3), churn/redundancy/kill-switch (T4). span gap marker (T1), logging (T3). Docstring/name nits folded into the touching task.
