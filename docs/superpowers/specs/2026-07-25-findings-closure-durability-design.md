# Findings closure durability — design

**Date:** 2026-07-25
**Status:** approved (brainstorming) → ready for implementation plan

## Problem

The final whole-branch review of `docs/superpowers/plans/2026-07-25-findings-pipeline-fix.md`
(commit range `932b6d0..2517740`) found that the repaired review-adjudication pipeline closes
findings once and then reopens them indefinitely, at a recurring Haiku cost that did not exist
before that branch (the path was previously dead).

Root cause, traced to the exact mechanism:

`store.record_finding`'s upsert (`store.py:2673-2681`) unconditionally does `resolved_at = NULL`
whenever the same `(finding_type, ref_id)` is re-detected. Its own docstring states this is
deliberate: *"a previously resolved finding resurfaces if it is re-detected."* That is correct
for the two original finding types (`project_no_next_action`, `area_overdue`), which have no
review/verdict process — "still detected" genuinely means "still unaddressed."

But every "no-mutation" verdict across all four `review_apply.py` appliers —
`keep`/`skip` (orphan), `external`/`skip` (missing_org), `waiting_on`/`unowned`/`skip`
(ownerless), `skip` (the bundled org checks) — calls `store.resolve_finding(finding_id)` and
nothing else. Nothing records that a decision was made. The lint check re-detects the exact
same unchanged row on the next run, `record_finding` upserts, `resolved_at` resets to `NULL`,
and `review.build_review_units` (`review.py:79-95`, which sources directly from
`store.open_findings(kind)`) pulls it right back into the next adjudication wave. Only the
`suppress`/`assign`/`owner`/`canonicalize` verdicts survive today, because those physically
mutate data that the check's own `WHERE` clause excludes — incidental, not designed. The same
hole exists for `memory_promotion` via `brain_finding_resolve`, added in the prior branch.

A second, smaller, related gap: `memory_distil.build_distil_requests` (`memory_distil.py:25-49`)
resubmits the same live notes to Haiku on every distil run with no "already reviewed" filter —
only `expire` marks the underlying chunk (`expired=True`) so `note_chunks` stops returning it.
A `keep` or `promote` verdict leaves the chunk metadata untouched, so the same note is
re-classified every run regardless of outcome.

## Approach

**Core mechanism: a nullable `verdict` column on `proactive_findings`, guarding the upsert.**

Considered and rejected: a separate `finding_acks` table plus a `LEFT JOIN` exclusion added to
every lint check's SQL and to `memory_distil`'s note selection (generalizing the existing
`entity_suppressions` pattern). Rejected because it touches many more files for the same
outcome — a settled finding still needs `record_finding` to stop reopening it either way, and
once that's guarded, a re-detected-but-guarded finding never reaches `open_findings()`, so no
adjudication ever prices it again. The verdict-column approach gets both effects (finding stays
closed; no further Haiku cost) from one change point, is non-breaking (a `None` verdict — every
caller this design doesn't touch — preserves today's behavior exactly), and needs no lint-check
or `memory_distil`-selection query changes at all for the finding-reopening half of the fix.

**Permanence of a verdict:** every terminal verdict, including `skip`/uncertain outcomes, sticks
permanently until manually cleared. Nothing about the underlying data changes between runs, so
re-asking an unchanged signal produces the same non-answer every time — pure recurring cost for
no new information. This mirrors how `entity_suppressions` already works: reversible via direct
row deletion (the existing "admin-delete-ok" convention), not a self-service tool.

**Manual dismissal also sticks.** The dashboard's `/api/dashboard/findings/<id>/dismiss` route
(`control_api.py:423`) currently calls plain `resolve_finding` with no verdict. It gains a
`dismissed_by_human` verdict — a person's deliberate action is at least as authoritative as an
AI `keep`/`skip`.

**`memory_distil` incrementality folded into this fix**, since it's the other half of the exact
same symptom for `memory_promotion`: even after a dismissed/promoted finding stops reopening,
the underlying note can still be re-asked-about and re-flagged by the distiller, landing on the
now-guarded (but still needlessly re-computed) finding.

## Component 1 — schema and store methods

`mcpbrain/store.py`:

- `init()`'s `CREATE TABLE IF NOT EXISTS proactive_findings` gains `verdict TEXT` (nullable,
  no default — absent on every existing row, matching "no verdict recorded" exactly). Existing
  stores get the column via the same additive-`ALTER TABLE ... ADD COLUMN` pattern already used
  elsewhere in `init()` (e.g. `thread_context.contextual_summary_at`), gated on a
  `PRAGMA table_info` check so it's safe to run against a store that already has the column.
- `record_finding`'s upsert `SET` clause changes:
  ```sql
  ON CONFLICT(finding_type, ref_id) DO UPDATE SET
    org         = excluded.org,
    summary     = excluded.summary,
    detail      = excluded.detail,
    severity    = excluded.severity,
    detected_at = excluded.detected_at,
    resolved_at = CASE WHEN verdict IS NOT NULL THEN resolved_at ELSE NULL END
  ```
  `verdict` here (unqualified) reads the row's *existing* stored value — `record_finding` never
  writes to the `verdict` column itself, only `resolve_finding` does. The insert branch's
  `VALUES` list is unchanged except appending `NULL` for the new column on a fresh row.
- `resolve_finding(self, finding_id: int, verdict: str | None = None) -> bool` — the `UPDATE`
  gains `, verdict=?` bound to the new parameter. Default `None` is fully backward compatible:
  any caller this plan doesn't touch behaves exactly as today (reopenable on redetection).
- `get_finding` adds `"verdict": r["verdict"]` to its returned dict, so review-applier tests and
  future callers can assert on it without raw SQL.

## Component 2 — `review_apply.py`: every terminal verdict passes its outcome

All 14 `store.resolve_finding(finding_id)` call sites become
`store.resolve_finding(finding_id, verdict=<outcome>)`:

| Applier | Call site outcome → verdict string |
|---|---|
| `apply_orphan_verdicts` | suppress success → `"suppress"`; keep → `"keep"`; skip/unrecognised → `"skip"` |
| `apply_missing_org_verdicts` | assign success → `"assign"`; external → `"external"`; skip/invalid-assign → `"skip"` |
| `apply_ownerless_verdicts` | owner success → `"owner"`; waiting_on → `"waiting_on"`; unowned → `"unowned"`; skip/invalid-owner → `"skip"` |
| `apply_org_verdicts` | canonicalize (ambiguous_org) → `"canonicalize"`; canonicalize (duplicate_org) → `"canonicalize"`; add_to_config → `"add_to_config"`; skip/anything else → `"skip"` |

The "missing" outcome branches (where the underlying entity/action/org field vanished between
detection and verdict) are unchanged — those leave the finding open, not resolved, so there is
no verdict to record.

## Component 3 — `mcp_server.py` and `control_api.py`

- `brain_finding_resolve`'s existing `store.resolve_finding(finding_id)` call
  (`mcp_server.py:~249`, added in the prior branch) becomes
  `store.resolve_finding(finding_id, verdict=outcome)` — `outcome` is already exactly
  `promoted`/`merged`/`dismissed`, no new vocabulary needed.
- `control_api.py:423`'s dashboard dismiss route becomes
  `store.resolve_finding(finding_id, verdict="dismissed_by_human")`.

## Component 4 — `memory_distil.py`: stop re-asking about settled notes

- New chunk-metadata field `distilled_at` (a timestamp string), set via the existing
  `patch_chunk_metadata` helper, mirroring how `expired` already works.
- `drain_distil` stamps `distilled_at` on **all three** verdicts:
  - `expire`: same `patch_chunk_metadata` call gains `distilled_at=now` alongside `expired=True`.
  - `promote`: a new `patch_chunk_metadata(doc_id, distilled_at=now)` call after the existing
    `record_finding` call.
  - `keep`: currently a bare `continue` with no chunk touch at all — gains
    `store.patch_chunk_metadata(doc_id, distilled_at=now)`, relying on `patch_chunk_metadata`'s
    own existence guard (returns `False` harmlessly for a stale/gone doc_id) rather than adding a
    separate pre-check.
  `now` is computed once per `drain_distil` call via `datetime.now(timezone.utc)`, matching
  `record_finding`'s own default-timestamp pattern.
- `store.note_chunks` gains `exclude_distilled: bool = False`, filtered in the same pre-`limit`
  Python loop as the existing `expired` check (so a distilled note can never fill the `cap` and
  starve a genuinely fresh one from being considered — the same reasoning that makes `expired`
  filtering happen before, not after, the limit).
- `build_distil_requests` passes `exclude_distilled=True`. `memory_index.py`'s call
  (`store.note_chunks(observation_type="memory")`, no `exclude_distilled`) is untouched — a
  distilled note must still render in the memory index; only the distiller itself stops
  re-asking about it.

## Testing

- Store-level: a finding resolved with a verdict does not reopen on a matching `record_finding`
  re-call; one resolved with no verdict (today's only path, still exercised by existing tests)
  still reopens — regression guard for the untouched default.
- One round-trip test per `review_apply` function: apply a no-mutation verdict, then re-run
  `record_finding` for the same `(finding_type, ref_id)`, assert `open_findings` does not
  include it and `get_finding(...)["verdict"]` matches the outcome string from the table above.
- `brain_finding_resolve`: extend the existing round-trip style test (from the prior branch) to
  also re-call `record_finding` after resolving and assert it stays closed.
- Dashboard dismiss route (`test_dashboard_digest.py::test_post_dismiss_finding`): verdict is
  `dismissed_by_human` and the finding does not reopen on redetection.
- `memory_distil`: `keep`, `expire`, and `promote` all stamp `distilled_at`;
  `note_chunks(exclude_distilled=True)` drops a stamped note even when it would otherwise fill
  the `limit`; `note_chunks()` (default, `memory_index`'s call shape) still returns it, unchanged
  from today.

## Out of scope, explicitly not fixed

- Time-boxed re-review (e.g. "ask again after 90 days") — permanence-until-manually-cleared was
  the deliberate choice, consistent with `entity_suppressions`'s existing reversibility model.
- A dedicated "unack" tool. Reversal remains a direct DB action (`UPDATE proactive_findings SET
  verdict=NULL, resolved_at=NULL WHERE id=?`, or clearing a chunk's `distilled_at`), matching the
  existing `entity_suppressions` convention of manual-only reversal.

## Files touched

- `mcpbrain/store.py` — schema (`verdict` column), `record_finding`, `resolve_finding`,
  `get_finding`, `note_chunks`
- `mcpbrain/review_apply.py` — 14 call sites across 4 appliers
- `mcpbrain/mcp_server.py` — `brain_finding_resolve`
- `mcpbrain/control_api.py` — dashboard dismiss route
- `mcpbrain/memory_distil.py` — `drain_distil`, `build_distil_requests`
- `tests/` — `test_store_schema_p3.py`, `test_review_apply.py`, `test_mcp_server.py`,
  `test_dashboard_digest.py` (covers `control_api.py`'s dismiss route —
  `test_post_dismiss_finding`, `tests/test_dashboard_digest.py:36`), `test_memory_distil.py`
