# Recency-as-source-of-truth for the knowledge graph

Status: proposed (2026-06-18)

## Problem

The store is bitemporal — relations and observations carry `valid_from` (the email's
date, not wall-clock) and `valid_to`/`invalidated_at`. But the code that *selects the
current value* and *supersedes old ones* is **processing-order / first-write**, not
`valid_from`-recency. Under live, roughly-chronological ingestion that coincides with
recency. Under **backfill walking years of history in arbitrary order, stale facts can
become "current."** Concretely:

1. **Singleton relations** (`works_at`/`reports_to`) — `upsert_relation` supersedes the
   existing current row with the *new* row's `valid_from` unconditionally
   (`graph_write.py`). An older email processed later makes the older employer current.
2. **Role** — `fetch_role` orders by `id DESC` (insertion order), so the last-*processed*
   role wins, not the latest-*dated*.
3. **Entity `org`** — `upsert_entity` writes org only when blank (sticky first-write),
   with no assertion date, so it's backfill-order-dependent and never updates after first
   set (a job change never propagates).

The fix is cheap because the temporal anchor (`valid_from` = email date) is already
stored — only the selection/supersession logic needs to honor it.

## Design

### 1. Date-aware singleton supersession (`upsert_relation`)
For a singleton relation, before inserting (a, rel, b):
- `same_target` current row → bump (unchanged).
- Find the newest *other-target* current conflict `newest_other` (max `valid_from`).
- `incoming_is_current = newest_other is None or valid_from >= newest_other.valid_from`.
- Insert/revive the (a, rel, b) row:
  - if current → `valid_to=NULL`, and supersede every other current conflict
    (`valid_to = valid_from`), as today.
  - if **not** current (an older fact arrived) → immediately mark the *new* row historical
    (`valid_to = newest_other.valid_from`, `invalidated_by = newest_other.id`), leaving the
    newer one current. The old fact is recorded but not authoritative.
Non-singleton relations are unchanged (they accumulate).

### 2. Date-aware role (`fetch_role` + `write_role_observation`)
- `fetch_role`: `ORDER BY valid_from DESC, id DESC` (newest-dated wins; id breaks ties).
- `write_role_observation`: retire prior same-source rows only when `valid_from <=` the new
  one; if a newer same-source current row exists, insert the incoming as historical
  (`valid_to` = that newer row's `valid_from`) so an older write never unseats a newer role.

### 3. Recency-aware entity org (`upsert_entity`)
- New nullable column `entities.org_valid_from` (schema migration in `store.init`).
- `upsert_entity` gains optional `valid_from: str = ""`. On an existing entity with a
  non-empty incoming org:
  - `valid_from` given → overwrite when `org` is blank **or** `org_valid_from` is null/older
    (`UPDATE … WHERE org='' OR org_valid_from IS NULL OR org_valid_from < ?`), setting both
    `org` and `org_valid_from`.
  - `valid_from` empty → unchanged only-if-blank behavior (back-compat for callers that
    don't pass a date).
- `apply()` passes `valid_from=lead_date_iso` at its `upsert_entity` call sites.
This also fixes "org never updates after first set."

### 4. One-off recompute pass (`maintenance/graph_cleanup.recompute_singletons`)
Corrects facts the backfill already scrambled, wired as a daemon one-shot
(`_graph_recompute_once`, meta flag `singleton_recompute_v1`) so every install heals:
- For each `(entity_a, relation)` in `{works_at, reports_to}` across ALL rows (current +
  superseded), pick the row with **max `valid_from`** (id breaks ties) as current
  (`invalidated_at=NULL, valid_to=NULL`) and mark all others superseded
  (`valid_to = max valid_from`, `invalidated_by = winner`).
- Role needs no data fix (selection-time). Org has no historical observations to recompute
  from, so it self-heals forward via §3 as entities are re-observed.
Idempotent.

## Non-goals
- Retrieval-time recency weighting of chunks (separate concern).
- Confidence/decay modelling beyond newest-wins.

## Tests
upsert_relation: older singleton arriving late stays historical, newer wins, revival
respects dates. fetch_role newest-dated wins regardless of insert order. upsert_entity org
overwrites on newer date, not older, blank-fills without a date. recompute_singletons picks
max valid_from and is idempotent.
