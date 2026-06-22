# Memory / Brain / Quality / Self-Improvement Roadmap

Date: 2026-06-22
Status: tracking plan (living checklist)
Companion artifact (full rationale + citations): https://claude.ai/code/artifact/749ef6c4-6aa2-486c-8c6d-97771d53220e

## Why this exists

Synthesised from (a) the live mcpbrain system, (b) what is already ported from
ops-brain/Nexus, and (c) 2024–25 research on agent memory, GraphRAG, brain-inspired
memory, and self-improving systems. Two governing findings:

1. **Three independent research lines converged on the same missing primitives** —
   salience-gated encoding, decay/forgetting, and offline consolidation. mcpbrain
   does the one thing no biological memory does: extract everything, keep all of it
   forever, equally weighted.
2. **Self-improvement only works with an *external* signal** (user behaviour, a human
   gold set, deterministic checks). Intrinsic self-critique degrades quality; tuning
   against an LLM-judge gets gamed. Anything we auto-tune must anchor to an external
   signal — never the model grading its own output.

## Ground truth: what is ALREADY built (do not rebuild)

Verified against current code on 2026-06-22 — much of the "obvious" roadmap is done:

- **Bitemporal fact validity + recency-aware supersession** — `valid_from`/`valid_to`/
  `invalidated_at`, and the ongoing write path already compares `valid_from` so a
  late-arriving older email cannot supersede a newer fact (`graph_write.py:419+`,
  `upsert_relation_bitemporal`). One-shot healing in `maintenance/graph_cleanup.py::recompute_singletons`.
- **Confidence + provenance** on observations/relations (`confidence`, `confidence_source`; `store.py:366+`).
- **Deterministic org-from-email** (`graph_write.org_from_email`) + taxonomy folding.
- **Deterministic entity merge** every cycle (`resolve.py::_deterministic_merges`) + fuzzy merge-review block.
- **Hybrid retrieval** — RRF fusion of vector + keyword/FTS, live in `/api/recall`
  (`retrieval.py::hybrid_search`), tunable rrf_k/vec_weight/kw_weight.
- **Live auto-recall** — UserPromptSubmit hook → daemon `/api/recall`
  (`prompt_recall.py::user_prompt_submit`), behind a flag, with an absolute
  vector-distance off-topic gate (commit `e1979ec`).
- **Synthesis/maintenance cadences** — profile synth, profile audit, thread synthesis,
  community synthesis (Leiden), daily lint, memory distillation (expire/promote).
- **Eval scaffold** — recall@k/MRR harness + regression floor (`tests/eval/`), but over a
  **synthetic, saturated** 25-query fixture (scores 1.0/1.0 under every setting).

## The real gaps (this plan)

Legend: `[ ]` todo · `[~]` partial/scaffold exists · priority `P0–P3`.

### Phase 0 — Foundations (stop the bleeding; start measuring)

- [ ] **P0 · Q1 Salience gate before extraction.** Source-aware `should_enrich()` ahead of
  the LLM. Today the only filter is the email-only lead-message noise check
  (`prepare.py::_filter_noise`); Drive (96% of the 66k backlog) and calendar get **zero**
  gating. Drive: skip prose-extraction for ~31k spreadsheets/CSV by `mime_type`, min text
  length, deprioritise shared-by-others. Email: use Gmail's own `CATEGORY_PROMOTIONS/UPDATES`
  labels, `no-reply@`, `List-Unsubscribe`. Low-salience → cold tier (embedded/searchable,
  never graph-extracted). *Biggest single lever; appears in every research stream.*
- [~] **P0 · S3 Real-corpus gold set.** Extend the synthetic eval (`tests/eval/`) with a
  **50–200 query→correct-memory** set over *actual* mail/docs so recall@k/MRR/nDCG measure
  real quality and tuning gains (the current fixture saturates and measures only structural
  regressions). RAGAS can seed; human-review.
- [ ] **P0 · S2 Recall acceptance instrumentation.** Log per auto-injected recall:
  used / referenced / edited-away / ignored. Cheapest external signal; the reward for all
  downstream tuning + a standing health metric. Hook: `prompt_recall.py` + a tray/daemon metric.
- [ ] **P1 · Q4 Org-backfill sweep.** `org_from_email` exists but 11,502 entities still lack
  an org — run a deterministic backfill pass over existing entities + audit which domains are
  missing from the taxonomy. Cheap, no LLM, directly cuts the lint findings.

### Phase 1 — Extraction & graph quality

- [ ] **P1 · Q2 Schema-constrained extraction + grounding check.** Validation/contract exists
  (`contract.py`); add a closed entity/relation type list and a per-triple grounding verifier
  ("is this explicitly in the source span?"). ODKE+: 91%→98.8% precision, −35% hallucination.
- [ ] **P1 · Q3 Entity resolution upgrade.** Add embedding-based **semantic blocking** (reuse our
  vectors) + a **cascade matcher** (rules → cosine → LLM-judge only on the ambiguous band) +
  edge dedup. Optionally restore the LLM-adjudication tier dropped in §9A. Mem0 write-time
  `ADD/UPDATE/DELETE/NOOP` against top-k neighbours to prevent re-fragmentation.
- [ ] **P2 · Q5 Drive-appropriate handling.** Fix `reassemble_thread` fragmenting multi-chunk
  Drive docs (~4.3k) into fake one-line "messages"; give docs a doc-shaped extraction
  (topics/decisions/references) vs the email prompt; skip tabular files (ties to Q1).
- [ ] **P3 · Q8 Stricter `validate_extraction`.** Empty/low-signal pushes currently count as
  "done" and mark the unit complete — stop that.

### Phase 2 — Brain layer (selective encoding, weighting, forgetting, consolidation)

- [ ] **P1 · B3 Importance scoring + three-axis recall.** Emit a 1–10 salience per extracted
  item (LLM poignancy + structural signals: known-person, reply-depth, user replied/starred,
  novelty). Rank recall by `relevance + recency_decay + importance` (we have relevance only).
- [ ] **P1 · B2 Tiered memory + always-injected core block.** Core (small durable facts —
  who Josh is, key people/orgs, standing commitments) injected every prompt; hot (consolidated
  semantic notes); warm (recent episodic); cold (low-salience/decayed — embedding-only,
  searchable on cue). Lets us "forget" ~55k from default recall while keeping 100% retrievable.
- [ ] **P2 · B4 Consolidation pass (sleep-time compute).** Upgrade `memory_distil` from
  expire/promote to RAPTOR-style: cluster recent episodic items → LLM-summarise into durable
  semantic notes that **cite sources**; trigger on accumulated-importance; interleave to avoid
  catastrophic forgetting. Reframe the daemon cadences explicitly as offline consolidation.
- [ ] **P2 · B5 Decay / forgetting with salience floor.** Strength `S` + last-access per memory;
  `R = e^(−Δt/S)`; on recall `S+=1, Δt←0`. Fold `R` into the recency term. Forget = demote not
  delete; high-importance / user-flagged items exempt.
- [ ] **P3 · B6 Memory typing** (episodic/semantic/**procedural** — extend `voice.md` into a
  procedural model of how Josh decides/drafts/delegates) **+ B7 incremental community extension**.

### Phase 3 — Retrieval polish + self-improvement loop

- [ ] **P1 · S1 Grounding/sufficiency gate before injecting recall.** Today's gate is
  vector-distance only — it cannot catch "similar but doesn't answer." Add a sufficiency check
  + NLI entailment; abstain on neutral/contradiction. (Insufficient context raised hallucination
  10%→66% in Google's study — a weak recall is worse than none.)
- [ ] **P2 · Q6 Retrieval polish.** We have BM25+RRF fusion; add **cross-encoder rerank** of the
  fused top-k and **query routing** (entity/multi-hop → graph local search; thematic → Leiden
  community global search; else hybrid). Communities exist but are unused for retrieval.
- [ ] **P2 · S4 Bandit threshold tuning + S6 abstention.** Thompson-sampling over 3–5 candidate
  recall thresholds (arms), reward = recall used (S2) — auto-tunes `recall_max_distance=0.80`.
  Verbalized confidence + conformal abstention, re-calibrated on schedule.
- [ ] **P2 · S5 Outcome-grounded "lessons learned."** Reflection that summarises *observed reality*
  (recall used/corrected, email confirmed an outcome, user edited X→Y), gated by an independent
  check before writing to memory. Our enrich loop is ExpeL-shaped; add the verification gate.

### Do NOT build (trap list)

- Self-critique loops with no external signal (degrade reasoning).
- Tuning against an LLM-judge with no human anchor (gamed by filler/verbosity).
- Fine-tuning/distilling on the system's own unfiltered outputs (model collapse).
  Structural safeguard (already true — keep it): enrichments are **additive over preserved
  real source**, never overwriting emails/notes.

## Bring-over from ops-brain/Nexus (gaps the port simplified)

- **Thread context reading** (`store.thread_context()`) — designed (Phase 3); `prepare._build_context`
  currently degrades to ''. Adds prior-thread narrative to extraction context.
- **Memory consolidation/synthesis** — Nexus consolidates related memories into summary notes;
  mcpbrain's distillation only expires/promotes. → folds into **B4**.
- **LLM entity-adjudication tier** (dropped §9A) — optional; merge-review block covers most. → **Q3**.
- Caveat: the ops-brain repo lives on the Nexus server (`/home/josh/ops-brain/src`), not on this
  machine or GitHub — these are inferred from in-repo design docs + "Nexus port" code comments,
  not a direct read. Direct access needed to confirm anything beyond the above.

## If you do only three things

1. **Q1 salience gate** — stop extracting junk (≈31k tabular Drive files + promotions) into the graph.
2. **S2 + S3** — instrument recall acceptance and build a real gold set, so we can *see* whether
   anything works (everything downstream tunes against these).
3. **S1 grounding gate** — stop confident bad recalls from being injected.

Most of the "stop surfacing stale facts" work (bitemporal) is **already done** — that frees Phase 0
to focus on the genuinely missing primitives above.
