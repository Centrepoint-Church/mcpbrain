# Org curator — LLM adjudication (real fuzzy-merge) design

**Date:** 2026-07-04
**Status:** Draft — design captured; implementation deferred until the local test
runner recovers (pytest currently 10–21× slower than normal, so a feature that
needs iterative test verification can't be built safely right now).
**Decision (Josh):** "Build real LLM adjudication now" — over deterministic-only or
a deterministic fuzzy threshold.

## Problem

`org_curate.adjudicate()` ships as an all-pending stub (`return []`). Deterministic
dedup (`resolve.resolve_entities(curator=True)`) already merges canonical-key and
email-equality duplicates, but genuine **fuzzy name-only** org duplicates ("Sam Lee"
vs "Samuel Lee", no shared email) are surfaced by `_build_adjudication_units` and then
never merged — so every consumer imports an org graph that slowly accumulates
duplicate person/org nodes. This is the exact "org graph goes bad" failure the system
exists to prevent.

Two pieces adjacent to this that fold in naturally:
- **B5:** member-contributed `merge_candidate` claims (`resolve.py` writes them into
  `org_contrib_outbox`) are staged but never consumed by the curator — they should
  become additional adjudication candidates.

## Key constraint: there is no synchronous LLM client

All model work in mcpbrain is **async via the enrichment spool**: `prepare` builds work
units → `brain_enrich_pull` hands them to a Claude Code subagent → the subagent extracts
→ `brain_enrich_push` → `drain` applies results. `brain-review` (Session-4 graph hygiene)
already adjudicates this way: `daemon._run_review` stashes review units as *block units*
for the enrich pipeline; a Claude subagent judges them; verdicts drain back and are
applied by `review_apply.py` on a later pass. There is **no** `messages.create`/`.complete()`
path to call synchronously.

Therefore curator adjudication must be **two-pass and async**, mirroring `brain-review` —
not a synchronous call inside `adjudicate()`.

## Design

### Flow (two curator passes, spool in between)

```
curator pass N:
  _ingest -> _materialise -> resolve_entities(curator=True)
  _build_adjudication_units(store)              # fuzzy pairs, structural-only
    + member merge_candidate pairs (B5)
  -> stash as block units "org_merge_review"    # into the enrich spool
  -> publish snapshot (without the still-pending fuzzy merges)

[enrich spool, between passes]
  brain_enrich_pull serves "org_merge_review" units to a Claude subagent
  subagent judges each pair -> {pair_id, verdict: merge|distinct|unsure, canonical?}
  brain_enrich_push -> drain stores verdicts (keyed by pair_id)

curator pass N+1:
  read drained verdicts -> _apply_merge_verdicts(store, verdicts, cap)   # EXISTING, hardened
  -> publish snapshot (now with the adjudicated merges applied)
```

`adjudicate()` is thus split: the **stash** half runs in pass N (queue units for the
spool), the **apply** half consumes drained verdicts in pass N+1. `_apply_merge_verdicts`
already exists with the full 0.7.84 hardening (re-fetch by own id, `_NAME_MERGEABLE_TYPES`
+ role-address guards, cap, self-pair guard, non-`merge` → no-op) — it is not rebuilt.

### What the subagent sees (privacy)

Only structural evidence, exactly what `_build_adjudication_units` already assembles:
`{pair_id, a:{id,name,type,email_addr,aliases}, b:{...}}`. **No message content, no
observations, no doc text** — there is no content in the org layer to leak. The prompt
instructs: merge only when confident the two nodes are the same real entity; default to
`unsure` (→ pending) on any doubt; never merge across types; treat differing non-empty
emails as strong evidence of *distinct*.

### Reusing the block-unit machinery

- Add an `org_merge_review` block kind alongside the existing `review_*` kinds (see
  `daemon._run_review`'s `kind_to_block_key` map and `review.build_review_units`).
- The units carry the structural pair packets; the enrich prompt/rules gain a small
  section for judging identity pairs (sync `enrich_prompt.md` → `plugin/agents/enrich-batch.md`
  via `bin/sync_agents.py`, per the release runbook).
- Verdicts drain into a store table (reuse `proactive_findings`/verdict plumbing, or a
  small `org_merge_verdicts` table keyed by `pair_id` with a consumed flag).

### Cadence

`_run_org_curate` already runs daily. The stash and apply halves both live inside
`org_curate.run()`: each daily pass applies any verdicts drained since last time, then
re-stashes any still-open fuzzy pairs. Convergence: a pair is judged once, applied on the
next pass; `merge` collapses it, `distinct` records a negative so it isn't re-queued
forever (add a `pair_id` suppression set, mirroring `entity_suppressions`).

## Idempotence / safety

- Re-stashing an already-queued or already-judged pair is a no-op (dedup on `pair_id`).
- `distinct`/`unsure`-with-repeat verdicts suppress re-queueing (no infinite re-ask).
- All merges go through the capped, reversible `_apply_merge_verdicts`; a bad verdict
  can't merge across types or onto a role address, and merges are logged in
  `entity_merge_log` (so a curator split can later undo — spec B5.3 rollback path).
- Publishing continues to work with fuzzy merges *pending*: they simply aren't applied
  until judged, so the snapshot is always valid, just occasionally carrying a not-yet-merged
  duplicate for one cadence cycle.

## Testing (to run once the environment recovers)

- **Stash:** `_build_adjudication_units` + member `merge_candidate` pairs are queued as
  `org_merge_review` block units; structural-only (no content) assertion.
- **Apply:** drained `merge` verdict → `_apply_merge_verdicts` collapses the pair;
  `distinct` → no merge + suppression recorded; `unsure` → pending, re-queued next pass
  but not infinitely (suppression after N repeats).
- **Guards:** verdict merging across types / onto a role address is refused; cap honoured.
- **Two-pass end-to-end:** member fixture with "Sam Lee"/"Samuel Lee" (no shared email) →
  pass N queues → simulated subagent verdict → pass N+1 merges → snapshot has one node.
- **Idempotence:** re-running a pass without new verdicts changes nothing.

## Out of scope

- Synchronous LLM calls (architecture is async spool by design).
- Merging beyond `_NAME_MERGEABLE_TYPES` (person/org/project).
- Auto-merging without adjudication (the deterministic path already handles the
  unambiguous cases; everything else is judged).
