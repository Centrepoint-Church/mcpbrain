# Brain hygiene pass — data-quality cleanup/refinement (ops-brain-informed)

> **Outcome after high-effort code review (2026-07-09).** The review caught
> real defects; final shippable set is 3 items, not 7:
> - **#4 stale-action TTL** — SHIPPED, hardened: only archives UNDATED, known-age,
>   non-snoozed open actions (never a dated/overdue commitment); ISO timestamps.
> - **#1 lint quality rates** — SHIPPED, fixed: uncapped orphan COUNT (the list is
>   LIMIT 50) + single duplicate-org scan.
> - **#7 suppression on merge** — SHIPPED, corrected: merge only CLEANS UP the
>   loser's suppression row; never repoints it onto the winner (that hid real,
>   more-connected survivors).
> - **#5 source-rank generalization — REVERTED.** The write-time guard dropped
>   legitimately-newer values from lower-rank sources while reads resolve by
>   recency, silently freezing stale attributes. Correct version needs paired
>   rank-aware reads; deferred. Original role-only gate retained.
> - **#3 stem-variant topic merge — REMOVED.** Derivational stemming over-merges
>   distinct topics (passion→pass, training→train); destructive. Embedding-based
>   topic clustering is the right tool; deferred.
> - **#2 / #6 — already-present / N/A** (see below).


Ported from a review of the retired `ops-brain` repo, scoped to what mcpbrain
does **not** already have. All items are reversible / no-hard-delete-by-default
and wire into the existing `lint_graph` report + daily cadences. Ships as one
release (0.7.94).

Deps already present: `rapidfuzz`, `nameparser`, `inflect` — no new deps.

## Item 1 — Extend `lint_graph` into a data-quality dashboard
`mcpbrain/lint_graph.py` already has: missing_org, orphan_entities,
ownerless_actions, ambiguous_org, duplicate_orgs, unenriched_emails.
Add (read-only, SQL, no mutation):
- `check_community_singletons` — communities with `member_count <= 1`.
- `check_stale_summaries` — entities with a summary older than N days (active only).
- a **stats header** in `build_report`: totals + orphan-rate + duplicate-rate +
  summary-coverage %.
Tests: `tests/test_lint_graph*.py` (seed rows, assert each check + header).

## Item 2 — Blocking + evidence-gated fuzzy duplicate detector
New `check_possible_duplicates(db)` in `lint_graph.py` (confirm none exists first):
- Block person entities by parsed **first name** (`nameparser.HumanName`, church
  honorifics added) and by **email addr**; score within blocks only (avoids O(n²)).
- `rapidfuzz.fuzz.token_set_ratio` base; **evidence gates**: surname overlap OR
  initial-abbrev when both have surnames; require org/domain/community
  corroboration when one is first-name-only; org/domain score adjustments.
- Threshold ≥75 → candidate (≥95 near-exact); cap 50; each carries a `reason`.
- Emits `lint:possible_duplicate` proactive findings (feeds existing review; never
  auto-merges).
Tests: blocking, surname gate, first-name-only corroboration, threshold routing.

## Item 3 — Stem-variant topic merge
Complement `topics.normalize_topic` (singularize + synonym map) with structural
suffix-stem merging in `consolidate.py` (beside `remap_topics`):
- Group topic entities whose names reduce to the same stem root (strip
  `ing/ation/tion/ment/ed/er/ion/s`, min root len 3).
- Merge via existing `store.merge_entities` (name-identity path); more-active
  node (email_count+degree) wins. Backup-gated (bin/consolidate.py subcommand
  `topics-stem`), reversible.
Tests: stem grouping, more-active-wins, no-op on distinct roots.

## Item 4 — Stale-action archive TTL
New `store.archive_stale_actions(cutoff_days=120, as_of=..., dry_run=False)`:
- Open actions whose source date (`COALESCE(source date, extracted_at)`) is
  older than cutoff → `status='auto_archived', resolved_by='ttl'`.
- **Never** archive an action with a future/empty-but-... deadline (`deadline`
  present and `>= today` → skip). Reversible (status flip). Idempotent.
- Wire a daily cadence (or bin/consolidate subcommand) — default ON, conservative.
Tests: archives old, skips future-deadline, skips recent, idempotent, dry_run.

## Item 5 — Generalize source-rank provenance in supersession
`graph_write.write_observation` currently applies `_source_rank` only as a *role*
write-gate. Generalize:
- On supersession, a lower-rank source must not overwrite a higher-rank current
  value for the SAME (entity, attribute) — skip (return) instead.
- Keep consolidation (observed_count) for equal value; keep recency within a rank.
- Read side (`fetch_role` / entity_detail): winner = max(source_rank, valid_from).
Tests: high-rank not clobbered by low-rank; equal-rank recency; consolidation intact.

## Item 6 — Orphan link-before-prune salvage
New salvage pass (in `maintenance/graph_cleanup.py` or `resolve.py`): for orphan
`document` entities (no links, no relations), search `email_context`/chunk
subjects for the doc name within ±7 days of first_seen; on match create the
email link + a `mentioned_with` relation (sender→doc) instead of leaving it
orphaned. Report count. Non-destructive (only adds links).
Tests: links a matchable orphan; leaves a non-matchable one; idempotent.

## Item 7 — Suppression survives merge
`store.merge_entities` repoints relations/observations/email but NOT
`entity_suppressions`. Add: `UPDATE OR IGNORE entity_suppressions SET
entity_id=winner WHERE entity_id=loser; DELETE leftover loser rows`. So a
suppressed entity can't reappear via a merge. Also repoint any corrections-like
tables if present.
Tests: merge carries a loser's suppression to the winner.

## Sequencing / gating
Order: 7 (tiny, store) → 5 (write path) → 4 (store) → 3 (consolidate) →
6 (salvage) → 1 → 2 (lint, share code). Each with scoped tests. Then full-suite
gate, version bump to 0.7.94, release runbook, update this machine.
All destructive ops backup-gated or reversible; lint/dup are report-only.
