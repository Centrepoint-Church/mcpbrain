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

## Bring-over from ops-brain (`itsjoshuakemp/ops-brain`)

Investigated directly against a clone on 2026-06-22. ops-brain is a 140-module system that
contains **production implementations of nearly every gap above** — most roadmap items become
*port + adapt* rather than *build from scratch*. Module names and the `evals/` gold set are
verified present; exact internals (line numbers, model choices, dep versions) should be
confirmed at port time. Portability constraint: ops-brain uses Qdrant + (in places) Voyage/Gemini;
mcpbrain is local-only (sqlite-vec + bge-small + Anthropic) — adapt the storage/model layer.

### Highest-value ports (map directly to the gaps)

| Gap | ops-brain module(s) | What it gives | Port |
|-----|---------------------|---------------|------|
| **S3 gold set** | `evals/golden_retrieval_set.yaml` (~30 hand-curated cases: `query` + `expected_chunk_ids` + `notes`), `evals/canary_queries.yaml` (regression thresholds from real usage), `evals/graders.py` (deterministic `CodeGrader` + LLM-judge `ModelJudgeGrader` pass@k + shell), `evals/run_evals.py` (baseline gating + email alert), `evals/cases/*.yaml` | A **real** gold set + grader harness (vs our synthetic, saturated 25-query fixture) | **Adapt** — swap Qdrant chunk-id lookup for our store; keep CodeGrader verbatim |
| **S2 acceptance signal** | `feedback.py` (record exposure/read/refinement, fire-and-forget) + `feedback_aggregator.py` (Bayesian-smoothed CTR, 90-day half-life) → `chunk_quality` multiplier in `scoring.py` | The exact recall-acceptance loop we need, already designed | **Adapt** — sqlite (we have it); wire into `retrieval.py` + `/api/recall` |
| **Q5 Drive extraction** | `multimodal_extract.py` (MIME router) + `spreadsheet_extractor.py` (sheet→markdown table), `pdf_layout_extractor.py`, `pdf_scanned_check.py` (avg <50 chars/page → scanned), `vision_extractor.py`, `slide_extractor.py`, `table_parser.py` | Per-type structural extraction instead of the email prompt over everything; `content_subtype`/`confidence`/`extraction_method` on chunks | **Adapt** — we already have pymupdf/openpyxl/python-docx; vision optional |
| **Q1 salience gate** | `ingest_gdrive.py::_is_mentioned_in_email()` — a Drive file becomes a graph entity **only if referenced in email**; everything stays searchable | A concrete, proven salience rule that cuts orphan-doc pollution at the source | **Adapt** — one mention-check before entity creation |
| **"better context"** | `contextual_summary.py` (Anthropic-style per-chunk context prefix before embedding) + `late_chunker.py`/`late_chunk_cutover.py` (token-pooled late chunking) | Each chunk vector carries document context — the user's "better context" ask | **Adapt** — Phase-1 contextual prefixes via Haiku is cheap; late chunking is heavier |
| **Q6 rerank + routing** | `colbert_indexer.py` (ColBERT late-interaction rerank), `router.py`/`long_form_router.py` (intent routing + graph seeding + compound decomposition), `crag_synonym_builder.py` + `query_signal_expansion.py` (corrective-RAG rewrite on low confidence), `scoring.py` (authority×quality×feedback weights) | Rerank, query routing, CRAG self-correction over our existing RRF fusion | **Adapt/inspiration** — ColBERT dep is heavier; routing + CRAG are portable |
| **S4/S5 self-improvement** | `evolution_db.py` (observability: query_log, retrieval_signals, gaps, experiments, eval_baselines, changelog) + `evolution_notify.py`; `query_signal_{calibration,gaps,runner}.py` (advisory tuning); `embedding_rot_monitor.py`+`rot_baseline.py` (drift alert with noise floor) | Outcome-grounded learning substrate + advisory tuning + drift detection — all **external-signal anchored** (safe per the research) | **Adapt** |
| **B3/B4 action lifecycle** | `cluster_actions.py` (deterministic), `tag_and_dedup_actions.py`, `triage_actions.py` (KEEP / CLOSE_EVENT / CLOSE_DUPLICATE / CLOSE_STALE), `action_reconciler.py` (pair open actions ↔ sent replies → mark done), `archive_stale_actions.py` (120-day TTL), `waiting_on_reconciler.py`, `triage_gates.py` | A full action lifecycle vs our 783 flat actions | **Adapt** — swap subprocess `claude` for our enrich harness |
| **B5 decay / B2 tiers** | `prune_hot.py` (age-based hot-tier decay), `hierarchy.py` (org/area/project tiers), `cache_warm.py`/`cache_invalidate.py` | Tiered memory + decay primitives | **Port** (`prune_hot` is tiny/deterministic) |
| **B6 procedural / voice** | `voice_analyser.py` (weekly, analysis-only) + `voice_samples.py` (multi-source authored samples) + `voice_apply.py` (guarded two-phase commit, cooldown, diff caps) | Procedural memory: learns how Josh writes from sent mail + edits + rejections | **Adapt** |
| **S5 safe self-refine** | `draft_planner.py → draft_classifier.py → draft_critic.py → draft_pretrial.py → draft_reviser.py` — one-pass, fallback-safe; **critic grounded in `voice.md` patterns + grounding checks** (external signal, not self-grading) | The exemplar of *safe* self-improvement: critic flags voice/coverage/grounding violations against a user-defined seed, reviser fixes only flagged issues | **Adapt** |
| **B4 thread context** | `contextual_summary.py` + `thread_context` reading (we degrade to '') | Prior-thread narrative into extraction context | **Adapt** |

### Notes

- **S3 + S2 + Q5 are the standout immediate wins** — ops-brain hands us a real gold set, the
  feedback loop, and proper document extractors, all of which we currently lack and would
  otherwise build from zero.
- The **draft critic** (`draft_critic.py`) is the template for every self-improvement loop we add:
  it grades against an external, user-authored signal (voice.md + grounding), never its own opinion.
- ops-brain confirms the **already-built** items too (bitemporal, org-from-email, synthesis,
  hybrid retrieval) — those were faithful ports, so no further bring-over needed there.

## If you do only three things

1. **Q1 salience gate** — stop extracting junk (≈31k tabular Drive files + promotions) into the graph.
2. **S2 + S3** — instrument recall acceptance and build a real gold set, so we can *see* whether
   anything works (everything downstream tunes against these).
3. **S1 grounding gate** — stop confident bad recalls from being injected.

Most of the "stop surfacing stale facts" work (bitemporal) is **already done** — that frees Phase 0
to focus on the genuinely missing primitives above.

---

## Post-implementation review — 2026-06-23 (capstone, all 4 phases, v0.7.63)

Adversarial end-to-end re-verification against the **live code** and the **live store**
(`~/Library/Application Support/mcpbrain/brain.sqlite3`, 595 MB, 80,584 chunks). Prior
per-phase "fixed" claims were treated as claims and re-checked. Tests: `pytest -m "not slow
and not e2e"` → **1 failed, 1647 passed, 1 skipped**; the single failure is the known
`test_restore_first_run::test_run_restores_before_migrate` daemon-lock flake (a live daemon
holds `daemon.lock`), not a code regression.

### Headline verdict

**The roadmap is impressively *wired* but operationally *dormant*.** Every one of the 17
features ships **default-OFF**, and `config.json` on the live install sets **none** of the
flags — so the live system runs plain RRF hybrid recall + the always-on salience/feedback
cadences and nothing else. More importantly, even the items that *do* run are starved of
substrate: the live store shows the brain layer has produced almost nothing.

**Live-store substrate (the decisive evidence):**

| Signal | Live state | Implication |
|---|---|---|
| `chunks.salience` | ~1,000 / 80,584 scored (**1.2%**); 79,584 still 0.0 | B3/B2/B5 all key off salience → ~99% blind |
| `chunks.memory_tier` | **all `''`** (0 core/hot/warm/cold) | B2 core block permanently empty |
| `chunks.memory_type` | **all `episodic`** (0 semantic) | B4 consolidation has produced nothing |
| `recall_feedback` | 108 rows, **all `exposure`; 0 `used`/`edited`** | the S2 "accept signal" has **never once fired** |
| `chunk_quality` | **0 rows** | S2 quality multiplier dormant (and not wired into ranking anyway) |
| `bandit_arms` | table exists, **0 rows** | S4 bandit has no reward → stuck at uniform prior |
| `recall_lessons`, drift tables | **do not exist** | S5 lessons + S4 drift have never run on this store |
| `community_summaries` | 35 detected, **0 titled, 0 summarised** | Q6 thematic routing has nothing to match |
| `draft_records` / `voice_suggestions` | 2 / 0 | B6 voice has no data to learn from |

So "shipped at v0.7.63" means **the scaffolding is built and tested; the engine has not been
started.** Maturity ≈ *code-complete, operationally cold-start.*

### Status table (17 items × 4 dimensions)

Legend — STATUS: ✅ shipped-and-wired · 🟡 wired-but-flag-off · 🟠 partial · 💤 inert-on-live · ⏸ deferred.
Issue map: Q1#5 Q2#9 Q3#10 Q4#8 Q5#11 Q8#12 / B2#14 B3#13 B4#15 B5#16 B6#17 / S1#18 S2#7 S3#6 S4#20 S5#21 Q6#19.

| Item | Status | Real vs wrapper (trade-down) | Activates on live store today? | Validated? |
|---|---|---|---|---|
| **Q1** salience gate | 🟡 flag off | Real after `source_type` re-key fix; Drive-mention rule opt-in. | No — `enrich_state` all `''`, 0 cold. | ❌ never run on live corpus; issue left open |
| **Q2** schema grounding | 🟡 flag off (RELATION_TYPES constraint **always-on**) | Deterministic token-overlap anchor; per-triple **LLM grounding deferred**. | Type-constraint yes; grounding filter no. | ❌ no live precision delta (ODKE #s are external) |
| **Q3** entity resolution / write-time dedup | 🟡 flag off | Cascade exact→token≥0.8; **embedding semantic blocking deferred** (no entity vectors). | No write-time dedup. | 🟠 problem measured (5,579 dup pairs) but fix-effect unmeasured |
| **Q4** org backfill | ✅ wired (daily) | Deterministic `org_from_email` — best-available deterministic. | Yes, runs daily. | ✅ audit-closed; cleanest item |
| **Q5** Drive extraction | ✅ wired | Real per-type (xlsx→markdown consumed by gate; OCR shipped); **slide-notes deferred**; OCR needs `tesseract` + Drive re-sync. | Yes, in extraction path. | 🟠 functional tests only, no recall metric |
| **Q8** stricter validate | ✅ wired (always-on) | Real; `enrich_attempts` cap stops re-queue loop. | Yes. | ✅ audit-closed |
| **B2** tiers + core block | 🟡 flag off → 💤 | Deterministic threshold tiers; core = top-N salience durable notes. | **No — 0 tiered chunks.** Even if flipped: needs salience (1.2%) + durable notes (0) → core stays ~empty. | ❌ |
| **B3** importance + 3-axis | 🟠 salience pass ✅ runs (not flag-gated); ranker 🟡 flag off (weights 0.0) | Structural-only default; LLM poignancy opt-in (`importance_llm` off). | Salience pass runs but **1.2% scored**; ranker off. | 🟠 gold 0.40→0.40 (no-regression only); importance axis never validated on real scores |
| **B4** consolidation | 🟡 flag off → 💤 | Single-level embedding-cosine cluster + claude-CLI summary; **not recursive RAPTOR**; lexical fallback. | **No — 0 semantic notes.** Trigger needs salience≥50 accumulated. | ❌ |
| **B5** decay | 🟡 flag off → 💤; **no audit on record (#16)** | Ebbinghaus `R=e^(−Δt/S)`, legit; **never-accessed chunks never decay** (no-op gap). | No demotion. | ❌ + unaudited |
| **B6** voice + incremental communities | 🟡 flag off → 💤 | Voice two-phase guarded (real); incremental community = 1-neighbour heuristic + full-Leiden-every-10. | Voice: 2 drafts/0 suggestions. Communities: full Leiden runs (35), 0 titled. | ❌ |
| **S1** sufficiency gate | 🟡 flag off — **audit-clean** | Real LLM relevance gate, but permissive ("prefer true"), fails-open, never returns empty; claude-CLI dependent. | No-op (off; also no-op without CLI). | 🟠 acceptance test proves withhold; no live sufficiency metric |
| **S2** acceptance signal | ✅ exposure wired (feedback ON) — but accept signal 💤 | **`used`/`edited` = within-session 24h re-recall proxy**, recorded *before* injection filter (can fire for never-shown docs). `chunk_quality` boost-only and **not wired into ranking** (weight 0.0). | 108 exposure, **0 used/edited, 0 quality**. | ❌ it *is* the signal others tune on — and it's empty/weak |
| **S3** gold set | 🟠 shipped, coverage-limited | ops-brain's set re-mapped; **10/30 coverable**; doc-level recall baseline **0.40 / MRR 0.16**; ground-truth mismatch from independent re-chunking. | Used by drift + Q6 gating tests. | 🟠 trustworthy enough for regression floors, **not** for absolute claims |
| **S4** bandit + drift | 🟡 flag off / advisory; **was dead-code, now has caller** (weekly `self_improve`) | Textbook Thompson TS (sound); drift logs **aggregate-as-per-case** (smell). Reward = the S2 re-recall proxy. | bandit_arms 0 rows; drift tables absent → **no reward ever produced**. | ❌ |
| **S5** lessons + draft critic | 🟡 flag off; lessons **was dead-code, now has caller** | draft_critic real but **LLM-judges-LLM**, fails-open-to-approve, voice rules hardcoded (not synced from voice.md). lessons = extract+verify (principled) but fed by the weak proxy; verifier is the **same model**. | recall_lessons table **doesn't exist** → 0 lessons; proxy never fired. | ❌ |
| **Q6** retrieval polish | 🟡 flag off — **measured to REGRESS** | regex routing; **token-overlap "rerank" ≠ cross-encoder**; keyword community augment; graph-seed concat; CRAG LLM rewrite. | Off; test gates them off until a cross-encoder beats baseline. | ✅ measured: routing+rerank **0.40→0.30** regression; correctly stays off |

### Cross-cutting assessment (the system as a whole, not 17 silos)

1. **Memory pipeline coherence — broken at the first link.** The chain
   salience(B3) → tiers(B2) → decay(B5) → consolidation(B4) → core-block is correctly *coded*
   end-to-end, but it is **starved at the source**: salience is 1.2% populated, so every
   downstream stage sees near-zero signal. With all of B2/B4/B5 flag-off as well, the pipeline
   is cold on both counts (no fuel *and* valves closed). Flipping the flags today would still
   yield an empty core tier and no consolidation (thresholds never met).
2. **Recall path — composes cleanly, mostly dormant.** `daemon.search()` layers distance-gate →
   (optional routing) → hybrid → 3-axis reweight → cold-exclude → decay-strengthen → sufficiency.
   The composition is correct and each layer self-gates/fails-open, so they don't fight. But with
   every layer off, the live path is **plain RRF** (`retrieval.py` weights default 0.0). The one
   measured composite (Q6) *regressed*, which is why it's gated off — a point in favour of the
   eval discipline.
3. **Self-improvement loop — fed by a proxy that has produced 0 events.** The "used" signal is
   within-session, 24-h-TTL re-recall (`prompt_recall.py:219`), recorded over raw results *before*
   the injection filter — so it is **not** evidence a human used anything. On the live store it
   has fired **zero** times in 108 exposures. The bandit (S4) and lessons (S5) are statistically
   and architecturally sound, but their input is both **weak** (a re-surfacing heuristic dressed
   as "outcome-grounded") and **empty**. `feedback.py` / `lessons.py` docstrings overclaim this
   signal. **Until a real accept signal exists, S4/S5 cannot produce useful tuning** — they are
   correct machines with no fuel.
4. **Gold set — trustworthy for regression floors, not for gating enablement.** 10/30 coverable,
   document-level baseline 0.40 after the re-chunking-mismatch fix. Good enough to catch a
   regression (it caught Q6's 0.40→0.30); **not** good enough to *prove* a feature helps. Any
   "enable because it improved recall" decision needs a re-seeded, mcpbrain-native gold set first.

### Adversarial sweep — confirmations & residual smells

- **Prior remediations that hold up in code:** Q1 `source_type` re-key (real), Q2 token-overlap
  grounding (real), Q3 once-per-drain index + org-merge-on-redirect (real), B2 `recompute_core`/
  `run_tier_pass` now live (real *code*, but inert on store), B4 `_cluster_by_embedding` (real),
  B6 full-Leiden-every-10 (real), S4/S5 now have a real caller (`self_improve` cadence — real,
  closes the dead-code finding). **None of these were found re-regressed.**
- **But "fixed" ≠ "working on live data" for 4 ex-inert items:** B2 core tier, S4 bandit/drift,
  S5 lessons, Q5 per-type tags were all *dead/inert* at audit; the remediations made the **code**
  live, and the live store confirms they **still produce nothing** (0 tiered, 0 lessons, 0 reward,
  0 semantic notes) — because the flags are off and/or the substrate is empty. The dead-code was
  fixed; the *inertness on real data* was not, and could not be by code alone.
- **Self-grading present but bounded:** draft_critic is LLM-judging-LLM (same claude CLI),
  fails-open-to-approve without the CLI. Lessons' "independent verifier" is an independent *call*,
  not an independent *model*. Neither is wired live, so low current risk — but both must stay
  anchored to an external signal before enabling.
- **claude-CLI dependence (fails-open/no-op when absent), not API-key:** S1, B3-LLM, B4 summary,
  S5 (both calls), Q6-CRAG. Plus **tesseract** host binary for Q5 OCR. On a CLI-less machine the
  "intelligent" layers silently degrade to nothing.
- **Schema fragility (uniform but real):** every LLM JSON parse is `find("{")`/`rfind("}")` +
  `json.loads` (sufficiency, voice, lessons×2, draft_critic) — defensively wrapped (failure →
  safe default) but brittle to braces-in-prose.
- **drift_monitor logs aggregate-mean as the per-case value** (`drift_monitor.py`) — the per-query
  table is fabricated from the aggregate; a correctness smell, not a crash.

### Enablement + validation plan (prioritised; what to turn on, in what order)

**Gate everything on a real substrate + a real signal first — do not flip brain-layer flags blind.**

**Tier 0 — prerequisites (no flags; do these before anything else):**
1. **Backfill salience over the whole corpus.** At 500/day the corpus needs ~160 days. Run a
   one-shot full `run_salience_pass` (or raise the cap) so salience is ~100% populated. *Nothing
   in B2/B3/B4/B5 is meaningful until this is done.* Measure the salience distribution after.
2. **Re-seed an mcpbrain-native gold set** from the actual corpus (not ops-brain's 10/30 remap).
   Target 50–100 query→doc cases with multiple acceptable docs. This is the prerequisite for
   *any* "it helps" claim.
3. **Build a real accept signal** (see Residual Risks #1). Until then S2's `used`/`edited` stays
   a proxy and S4/S5 stay advisory-only.

**Tier 1 — safe, cheap, validate-then-enable (after Tier 0):**
- **B3 importance ranker** (`importance_recall`): enable *after* salience backfill; A/B on the new
  gold set (expect recency to help, importance axis to be the real test). Keep `importance_llm` off
  until the structural axis is proven.
- **Q1 salience gate** (`salience_gate`): enable after measuring gated-vs-kept counts on the live
  corpus (the issue explicitly left this open). Start without `salience_require_drive_mention`.
- **S1 sufficiency gate** (`sufficiency_gate`): audit-clean and fail-open; lowest-risk LLM layer.
  Enable once claude-CLI availability on the daemon host is confirmed; watch withhold-rate.

**Tier 2 — only after Tier 1 shows gains:**
- **B2 tiers + core block** — needs salience backfill *and* some durable/semantic notes to exist
  (so core isn't empty). Enable B4 first to create semantic notes, then B2.
- **B4 consolidation** — needs salience populated (trigger is salience≥50 accumulated) and a
  claude-CLI host. Validate the cited-summary quality by hand on the first batch.
- **B5 decay** — fix the never-accessed-never-decays gap first; **commission the missing audit
  (#16)**; enable only after access data accumulates and B2 is on (so "forget" = demote-to-cold).

**Do NOT enable yet (and why):**
- **Q6 routing / rerank** — *measured to regress* (0.40→0.30). Off until a real cross-encoder is
  added and beats baseline. CRAG/contextual-prefix can be evaluated separately.
- **S4 bandit auto-apply** (`bandit_auto_apply`) and **S5 lessons** (`lessons`) — both consume the
  re-recall proxy; with 0 real accept events they would tune/learn from noise. Off until Residual
  Risk #1 is solved.
- **Q3 write-time dedup** (`write_time_dedup`) — measure recall/merge correctness on a sample
  before trusting it to redirect writes silently.

### Residual risks + top 5 highest-leverage next pieces of work

1. **Build a genuine recall accept signal** (the single highest-leverage item). The current
   within-session re-recall proxy has produced 0 events and doesn't mean "useful." Options:
   an explicit "was this recall useful?" affordance, edit-distance between injected recall and the
   user's eventual output, or a citation/quote-back detector. Everything in S2/S4/S5 is blocked on
   this. **Correct the `feedback.py`/`lessons.py` docstrings** that currently overclaim it.
2. **Full salience backfill + a one-shot tier/consolidation bootstrap.** The brain layer is cold
   because salience is 1.2% populated and 0 durable notes exist. A single backfill run + one
   consolidation pass would turn B2/B3/B4 from "wired-to-nothing" into "has substrate to evaluate."
3. **Re-seed the gold set from the mcpbrain corpus** (10/30 coverable, ground-truth-mismatched is
   too thin to gate enablement). Allow multiple acceptable docs; verify expected ids against the
   live chunking.
4. **A real cross-encoder reranker** to replace the token-overlap stand-in — the only path that
   makes Q6 rerank net-positive (it currently regresses). Weigh the optional model dep against the
   local-only constraint.
5. **Core-tier seeding + the B5 never-accessed gap + the missing B5 audit.** Seed core from
   known identity/standing-commitment notes so the always-injected block is useful on day one;
   fix decay's no-op on never-accessed chunks; commission the audit #16 never received.

## Post-review remediation — 2026-06-23 (acting on the capstone review)

Acted on the review's Tier-0 + safe fixes. All code below is flag-gated as before
(production behaviour unchanged); the substrate work ran on the live store. Suite:
`-m "not slow and not e2e"` → 1655 passed, 1 skipped, 1 known daemon-lock flake.

**Safe correctness/honesty fixes (shipped):**
- **Accept signal upgraded to quote-back** (residual risk #1). Replaced the within-session
  re-recall proxy (recorded over raw results *before* the injection filter; fired 0×) with a
  deterministic transcript check: an injected snippet is credited `used` only when its distinctive
  words later reappear in the assistant's response (`prompt_recall._detect_quoteback`, ≥60% token
  containment, only ever scoring snippets actually injected). It is a *behavioural* proxy, not a
  human judgement — `feedback.py`/`lessons.py` docstrings rewritten to say exactly that (one had
  *under*-claimed "not captured", the other *over*-claimed "a user actually used"). S4/S5 still
  shouldn't be enabled until this accrues real volume, but the signal is now honest and non-spurious.
- **B5 decay never-accessed gap fixed.** `apply_decay_pass` no longer exempts never-recalled chunks
  forever; it anchors their age on the source date (`modified`/`date`/`start`) parsed from metadata,
  staying conservative (skip) only when no date is parseable. (B5 audit #16 still owed.)
- **drift_monitor per-case fabrication fixed.** Was writing one row per gold case all carrying the
  aggregate mean — which also corrupted the 30-*row* baseline window (N rows/run). Now one honest
  aggregate row per run.

**Salience backfill (substrate, ran on live store) + a calibration finding:**
- Backfilled structural salience over **100%** of the 80,585 embedded chunks (was 1.2%), purely
  deterministic, 3s, no LLM. **In doing so, found the scorer was mis-calibrated for the live
  metadata** and fixed it: (a) the structured-content bonus checked `source_type in
  (calendar/google_drive/drive)` but live Drive is `gdrive` → all 64k Drive chunks missed it;
  (b) `_parse_age_days` read `date`/`start` but Drive carries its date in `modified` → all 64k got
  no recency; (c) added owner-authored detection from `sender`==owner_email / `SENT` label (live
  ingest never set `sender_is_owner`). Distribution went from 95.7% pinned at 3.0 → spread 1.0–6.5,
  avg 3.47.
- **Open finding: the high band is still empty — max salience ≈ 6.5, 0 chunks ≥ 7.0.** Reaching ≥7
  structurally needs owner-authored content from the *last 7 days* (sparse), and the 9.7k enriched
  chunks carry no date/sender at all. Consequences: B5's `_FLOOR_SALIENCE=7` exemption protects
  nothing on this corpus (recalibrate to ~the top percentile, or make it a percentile); and the
  **core tier cannot form from salience** — `top_core_candidates` requires `memory_type IN
  (semantic,procedural)` and the store is 100% `episodic`. So **core is blocked on B4 consolidation
  (which creates semantic notes), NOT on salience.** That is the real next domino.
- **Measured consequence on the gold set:** with salience now populated, the B3 three-axis ranker
  (recency+importance+decay) **regresses recall@10 0.40→0.30** on the (thin) gold set — previously
  this test passed only because salience was empty and the importance axis was inert. The test was
  restructured (mirroring the Q6 test) to assert `importance_recall` stays **default-OFF** until a
  better gold set + tuned weights beat baseline.

**Gold set re-seed — the review's assumption was wrong, in an informative way:**
- The 10/30 coverage is **not** repairable id-drift. The live gmail doc_id format is *identical*
  to the gold set's, yet the referenced messages/files simply aren't here — the gold set is
  **ops-brain-native** and mcpbrain holds a different (overlapping) corpus. Deterministic re-anchoring
  is impossible; a real set must be **authored from live documents**.
- Seeded a **20-case mcpbrain-native candidate** (`tests/eval/golden_retrieval_set_mcpbrain_candidate.yaml`,
  generator `tests/eval/seed_gold_candidate.py`): distinct verified-present docs, spreadsheets
  skipped, near-dups collapsed, queries written by the claude CLI from each doc's subject+content
  and instructed not to echo the title (so it tests *semantic* retrieval). **20/20 coverable,
  baseline recall@10=0.75 / MRR=0.32 — not saturated**, so it can actually detect quality changes.
  Left **pending human review** and NOT wired into the harness (the misses expose ambiguous
  ground-truth clusters — e.g. four "CP College Semester Two" emails — that a human should resolve
  with multiple acceptable docs). This is the prerequisite for any "it helps" enablement claim.

**Revised enablement order (substrate now exists):**
1. Curate the mcpbrain-native gold set candidate → trust it for gating.
2. **B4 consolidation** next (not B2): it is the domino that creates the semantic notes B2's core
   tier needs. Validate cited-summary quality by hand on the first batch.
3. Then **B2 core** (now non-empty) and re-test **B3 importance ranker** on the curated gold set
   (off until it beats baseline). Recalibrate B5 `_FLOOR_SALIENCE` to the real distribution before
   enabling decay. Keep Q6 rerank / S4 auto-apply / S5 lessons off per the review.

### Execution plan — 3 review-gated sessions (2026-06-24)

The enablement order above is executed as **three sessions, each in a fresh Claude Code
context, with a review checkpoint back in the originating session between each**. A session
STOPS at its boundary and reports measured numbers; the next session does not start until its
predecessor's output is reviewed. Ordering is load-bearing (each session consumes the prior's
substrate). Standing constraints apply throughout: subscription-only (claude CLI), measure on
the gold set before/after, no test-floor loosening, no self-grading, schema-safe reads, all new
behaviour flag-gated default-OFF, commit per session but **never push without explicit instruction**.

- **Session 1 — Commit + gold set (foundation & measurement).** Commit the prior session's
  remediation (quote-back signal, scorer calibration, decay/drift fixes, docstrings, candidate
  gold set, doc addendum) on a branch; curate the 20-case mcpbrain-native candidate (resolve the
  ambiguous ground-truth clusters → multiple acceptable docs, verify ids against the live store);
  wire it in as the gating set; re-baseline the drift monitor. *Deliverable:* final case count,
  ~100% coverage, baseline recall@10/MRR. Low-risk; produces the eval everything else gates on.
- **Session 2 — B4 → B2 (consolidation + core tier).** Bootstrap one consolidation pass on the
  live store to create cited `semantic` notes; HAND-VALIDATE the first batch before enabling
  `consolidation`; then form + enable the **B2 core tier** (now non-empty) and verify the
  always-injected /api/core block. *Deliverable:* # semantic notes + citation-integrity check,
  core-tier size + sample core block. Riskiest (live LLM on real data) → its own session.
- **Session 3 — B4/B2 quality prereqs, then B3/B5 + Q1/S1 (measure-then-enable).** FIRST: (a) make
  the gold eval/gating use the production `exclude_cold=True` path; (b) tighten B4 clustering so
  consolidation notes are topically coherent (not one mega-cluster) + seed the core tier from
  durable identity/standing-commitment notes so the always-injected block is useful. THEN:
  re-measure the three-axis ranker on the curated gold set (enable `importance_recall` only if it
  beats baseline, else keep off + document); recalibrate B5 `_FLOOR_SALIENCE` to the real
  distribution (max ≈6.5) + do the missing #16 audit, then enable `decay`; measure + enable Q1
  `salience_gate` and S1 `sufficiency_gate`. Keep Q6/S4/S5 OFF. *Deliverable:* gold-set
  before/after, new floor value, flag states → append an "Enablement log" section here + comment
  on epic #22.

**Session 1 — DONE (2026-06-24, branch `session1-gold-set-foundation`, commit `2c04317`).**
Prior remediation committed (+ a robust fix to a time-bomb in `test_probes.py`); the 20-case
mcpbrain-native gold set curated (ambiguous CP-College and Capes-finance clusters cross-linked to
multiple acceptable docs, all 20 ids verified present) and wired in as the gating set
(`load_gold_cases()` prefers it; floors set to regression levels GOLD_RECALL_FLOOR 0.55 /
GOLD_MRR_FLOOR 0.20 / MIN_COVERED 15). **Finding: curation did NOT move recall (still 0.750 /
MRR 0.322 over 20/20 coverable)** — so the 5 misses are *genuine retrieval gaps* (the cross-linked
siblings also fail to rank in top-10), confirming the set is a valid, non-saturated gate rather
than a ground-truth artifact. Sessions 2-3 build on this branch.

**Session 2 — DONE (2026-06-24, branch `session1-gold-set-foundation`).**

B4 consolidation + B2 core tier bootstrapped on the live store; gold-set recall **unchanged at
0.750 / MRR 0.322 over 20/20** — no regression from either change.

*B4 — One consolidation pass:* ran directly (bypass config flag for validate-before-enable).
Embedding-based clustering (cosine ≥ 0.55) over the 50 highest-salience episodic chunks →
1 cluster → 1 semantic note (`note-consolidated-7c4a9559bbd28d9e`, 1226 chars, 50 source chunks
promoted to hot). Hand validation of 4 cited claims against their source chunks — all accurate
and grounded:
  - "bulk-entering Dance Inclusion's classes… asked colleagues to disregard notification emails
    [gmail-19eceff4d859c3dd-body-0]" → source confirms verbatim.
  - "coordinated a 20-ticket allocation with Lisa… Lisa Rossi having sent the remaining names
    [gmail-19ecf176567b27d4-body-0]" → source confirms Lisa Rossi and chasing names.
  - "managed risk assessments (Pilbara… Annual Playgroup)… distribute responsibility across the
    team [gmail-19ecf2e4b5817328-body-0][gmail-19ecf7b8f8340c5a-body-0][…0ea6a948b-body-0]"
    → all three sources confirm verbatim.
  - "offered Lauren a discounted School of Ministry place… gap year, via Ps Edward, Ps Taryn
    [gmail-19ecf634558d110d-body-0]" → source confirms Lauren, gap year, Ps Taryn/Edward.
  Citation integrity: pass. No hallucinations detected in verified claims.
  Flag: `consolidation: true` set in config.json.

*B2 — Core tier:* `recompute_core()` ran → 1 durable semantic note → core tier. `run_tier_pass()`
ran (promoted=0, demoted=1309 to cold, core=1). Bug fixed: `store.core_chunks()` was counting
raw text length (1200+ chars) against the 700-char budget before `get_core_block`'s 200-char
snippet truncation, so the only semantic note was silently dropped; fix uses
`min(len(snippet), 200)` as the budget contribution. `/api/core` verified to return real content
after daemon restart. `prompt_recall.py` prepends the core block to every recall response.
Flag: `tiered_memory: true` set in config.json.

*Cold-tier demotion:* 1309 chunks (salience < 3.5) demoted to cold in first tier pass — gold-set
recall unchanged, confirming cold-exclude is not cutting relevant chunks.

*Known limitation:* Only 1 semantic note exists (one bootstrap pass). The daily `consolidation`
cadence will create more on subsequent passes. The core block content is currently operational
notes from Josh's recent email cluster — useful but narrow. More passes → broader coverage.

**Session 2 — REVIEW (2026-06-24, Opus).** Approved, with two findings carried into Session 3:
1. *Validation path corrected (result positive).* The gold eval ran with the default
   `exclude_cold=False`, but the production recall path sets `exclude_cold=True` when
   `tiered_memory` is on (`daemon.search`). Re-run on the production path: recall holds at **0.750**
   and **MRR improves 0.322 → 0.483** — cold exclusion is net-positive (pushes low-salience noise
   out of top ranks, drops no gold doc). ACTION: the gating test + all Session-3 measurements must
   use the `exclude_cold=True` path so they reflect what users actually get.
2. *Consolidation quality — the always-injected core block is a grab-bag.* Clustering produced
   **one cluster of all 50 chunks** (cosine ≥ 0.55 over top-50-salience, which are all recent owner
   ops-email → everything merges), so the one note crams four unrelated topics into a paragraph.
   Accurate, but it is a *digest of one week's email*, not a durable fact — and more passes will
   just make more grab-bag notes. ACTION (Session 3 prereq): tighten/finer-grain B4 clustering so
   notes are topically coherent (raise threshold and/or cap cluster size), and **seed the core tier
   from durable identity/standing-commitment notes** (e.g. the voice/profile model) so the
   always-on block is useful, not transient.

Session 3 to follow (B3/B5 + Q1/S1, preceded by the two fixes above).

---

## Enablement log — 2026-06-24 (Session 3)

Branch `session1-gold-set-foundation`. All measurements on the **production path
(`exclude_cold=True`)** — the path `daemon.search()` uses when `tiered_memory` is on.
Gold set: 20-case mcpbrain-native set, 20/20 coverable.

### Prereq 0a — Production eval path corrected ✅

`test_gold_recall_floor` and the B3 gating test now pass `exclude_cold=True` to
`gold_eval`. GOLD_MRR_FLOOR raised 0.20 → 0.35 to reflect the production-path
baseline (MRR=0.483 vs prior 0.322 on the cold-inclusive path). Recall floor
unchanged at 0.55 (production-path recall=0.750, same as non-production).

### Prereq 0b — B4 clustering quality + core seeding ✅

**Clustering fix:** `_cluster_by_embedding` threshold raised 0.55 → 0.75; cluster
size capped at 10 (`_MAX_CLUSTER_SIZE`). One consolidation pass over the 50 highest-
salience episodic chunks → **6 clusters → 5 topically coherent notes written**
(all about Centrepoint College recurring schedule, different time windows; 5th cluster
had < `_MIN_CLUSTER_SIZE` chunks after capping). Citation check on 3 notes: all cited
source IDs are present and content is grounded — PASS. No grab-bag merge.

**Core seeding:** `seed_core_identity(store, home)` added to `memory_tier.py`.
Synthesises durable identity note from `config.json` owner fields + `records/context/identity.md`,
writes as `note-core-identity-seed` (stable doc_id, idempotent), `memory_type='semantic'`,
`salience=6.5`. `core_chunks` ordering changed to `salience DESC` so the identity seed
always appears first in the always-injected block.

`/api/core` block now leads with:
```
- Joshua Kemp — Operations Manager at Centrepoint Church, Courageous Church, ACC
  Email: josh.k@centrepoint.church
```
followed by the Session-2 grab-bag note (salience=3.0) and one college-schedule note.
Core tier size: 8 (identity seed + 7 semantic notes). **No recall regression.**

**Gold eval after prereqs:** recall@10=0.750 / MRR=0.483 (production path) — unchanged.

### B3 — Three-axis ranker (importance_recall) ✅ ENABLED

| Path | recall@10 | MRR | covered |
|---|---|---|---|
| Baseline (production, `exclude_cold=True`) | 0.750 | 0.483 | 20 |
| + three-axis weights | 0.750 | **0.571** | 20 |
| Delta | +0.000 | **+0.088** | — |

On the production path the three-axis ranker does **not regress recall and lifts MRR
by +0.088** (+18%). Prior finding (Session 2 review) that it regressed was on the
non-production path (`exclude_cold=False`). Flag: **`importance_recall: true`** set in
`config.json`. `importance_llm` stays OFF (structural salience sufficient for this win).

### B5 — Decay floor recalibrated ✅ / Decay flag HELD

**Floor recalibrated:** `_FLOOR_SALIENCE` 7.0 → **6.0** in `decay.py`.
B5 audit (2026-06-24): corpus 80,705 chunks, max salience=7.0 (1 chunk), 48 chunks
at ≥6.0 (0.06%). At 7.0 the floor exempted only 1 chunk; at 6.0 it protects the
genuine high-salience band (consolidated semantic notes + identity seed + recent
owner-authored content with engagement signals).

**Decay flag HELD (not enabled):** Dry-run shows 85% of corpus (≈67,855 / 80,000)
would decay to cold in one pass. Root cause: 0 chunks have `last_accessed` data
(the quote-back accept signal has not yet accumulated real events), so all aging is
anchored on source-date. With strength=5.0 (initial) any chunk older than ~7 days
would decay (R < 0.25). Additionally, 4 of 35 gold-set expected chunks would decay,
risking recall regression that cannot be measured without running the pass. Decision:
enable decay after the quote-back signal accumulates meaningful `last_accessed` data
and the impact on the gold set can be validated. The never-accessed fix (source-date
anchor) and the floor recalibration are shipped; only the flag is held.
Flag: **`decay`: OFF** (unchanged).

### Q1 — Salience gate ✅ ENABLED

Measured gated-vs-kept on a 10,000-chunk random sample of the live corpus:
- Kept: 6,006 (60.1%) — prose email + Drive documents
- Gated: 3,994 (39.9%) — almost entirely `gdrive` tabular/short-text files
- Extrapolated to corpus: ≈32,233 / 80,705 chunks would be gated from re-extraction

The cut is sane: the gate prevents re-LLM-extraction of spreadsheets, CSVs, and
near-empty Drive stubs. Gated chunks stay embedded and searchable (`enrich_state='cold'`
≠ `memory_tier='cold'`; does not affect recall at query time). `salience_require_drive_mention`
stays OFF (mcpbrain holds valuable un-emailed docs that would be wrongly excluded).
Gold eval unchanged (gate affects extraction queue only, not recall).
Flag: **`salience_gate: true`** set in `config.json`.

### S1 — Sufficiency gate ✅ ENABLED

Claude CLI confirmed available: `/Users/joshkemp/.local/bin/claude` v2.1.187.
Gate design: NLI-style batch call, permissive (defaults `relevant=true` on uncertainty),
fails-open on CLI absence / timeout / parse failure, never returns empty.
Withhold-rate: to be monitored from daemon logs (`sufficiency gate: kept N/M hits`).
Flag: **`sufficiency_gate: true`** set in `config.json`.

### HELD (do not enable)

| Feature | Flag | Reason held |
|---|---|---|
| Q6 routing/rerank | `retrieval_routing`, `retrieval_rerank` | Measured regression 0.750→? (token-overlap reranker weaker than plain RRF); needs real cross-encoder |
| S4 bandit auto-apply | `bandit_auto_apply` | 0 real accept events; would tune from noise |
| S5 lessons | `lessons` | 0 real accept events; verifier is same model (not independent) |
| B5 decay | `decay` | 0 `last_accessed` data; 85% corpus would decay immediately; 4 gold expected docs affected |

### Final live config.json flag states

```
consolidation:     true   (Session 2)
tiered_memory:     true   (Session 2)
importance_recall: true   (Session 3, measured +0.088 MRR on production path)
salience_gate:     true   (Session 3, 40% cut — sane)
sufficiency_gate:  true   (Session 3, CLI confirmed, fails-open)
decay:             OFF    (floor recalibrated; flag held pending access data)
importance_llm:    OFF    (structural salience sufficient)
retrieval_routing: OFF    (regresses)
retrieval_rerank:  OFF    (regresses)
bandit_auto_apply: OFF    (no real signal)
lessons:           OFF    (no real signal)
write_time_dedup:  OFF    (not validated)
```

**Session 3 — REVIEW (2026-06-24, Opus).** Approved. Independently reproduced the headline:
on the production path (`exclude_cold=True`) the three-axis ranker holds recall@10=0.750 and
improves MRR 0.483→0.571 (+0.088) — the B3 enable is justified. Verified the enabled flags in the
live config (`importance_recall`, `salience_gate`, `sufficiency_gate`, `consolidation`,
`tiered_memory`). B5 decay correctly HELD (would cold-tier ~85% incl. 4 gold docs with no
last_accessed data). Two findings:
1. *S1 over-abstention check (the report omitted it):* measured `filter_by_sufficiency` on 10 gold
   queries → **0/10 withheld** any hit, so S1 is not suppressing answerable recall (fails-open +
   permissive, as designed). Enable confirmed safe. (Note: `gold_eval` doesn't route through S1, so
   its number doesn't reflect the gate — acceptable since S1 doesn't drop answerable hits.)
2. *Drifted gate FIXED:* the three-axis test only asserted the code default stays OFF — it no longer
   guarded the now-ENABLED ranker's quality, so a future regression would pass silently. Rewrote it
   to `test_gold_three_axis_does_not_regress_on_production_path`: asserts the enabled ranker does not
   regress recall@10 OR MRR on the production path (+ still guards the new-install default OFF).

**All three sessions complete.** Brain layer is live and measured: salience-ranked recall with an
always-injected durable identity core, cold-tier forgetting, source-grounded consolidation, and a
salience extraction gate — every enable gold-set-validated; decay + the self-improvement loop
(S4/S5) held until real accept-signal volume accrues. Branch `session1-gold-set-foundation` is
unpushed (awaiting explicit ship instruction).

## Contextual retrieval (Q6) — validated + flag made honest (2026-06-24, post-sessions)

Found during a flag audit: `contextual_retrieval` was a **dead, misleading knob** — its
docstring said "Default: False — enable via config", but `index.py::index_pending` applied the
provenance prefix (`embed.contextual_prefix`) to **every** chunk *unconditionally* and never read
the flag. So the whole live corpus was already contextual-embedded, with zero measurement.

**Validated it (controlled A/B, vector channel, 4,018-chunk sample incl. all 20 gold docs, same
docs embedded both ways):**
- WITHOUT prefix: recall@10 **0.850**, MRR **0.566**
- WITH prefix:    recall@10 **0.950**, MRR **0.741**  → **+0.10 recall, +0.175 MRR**

A clear, large win — the always-on behaviour was correct. **Fix:** made the flag real and
**default TRUE** (preserves current behaviour, adds a rollback switch), gated `index_pending` on
it (`home` selects the config), and corrected the docstring with the A/B numbers. Behaviour is
unchanged for every existing test; the only new capability is the ability to disable + re-index.
Added `test_index_pending_prepends_contextual_prefix_by_default` and
`test_index_pending_respects_disable_flag`.
