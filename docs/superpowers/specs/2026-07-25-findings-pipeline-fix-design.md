# Proactive-findings pipeline fix — design

**Date:** 2026-07-25
**Status:** approved (brainstorming) → ready for implementation plan

## Problem

The live store holds **200 open `proactive_findings`** that never close. They are not
200 distinct problems — they are three separate lifecycle failures, plus a reporting
cap that hides the real backlog.

Open findings by type, measured on the live store:

| Finding type | Open | True (uncapped) count |
|---|---|---|
| `lint:orphan_entity` | 50 | 404 |
| `lint:missing_org` | 50 | 63 |
| `lint:possible_duplicate` | 50 | — check deleted |
| `lint:ownerless_action` | 26 | 242 |
| `memory_promotion` | 16 | — |
| `lint:ambiguous_org` | 3 | 3 |
| `lint:duplicate_org` | 3 | 3 |
| `org_unrecognised` | 2 | 2 |

The 50s and the 26 are `LIMIT 50` / `LIMIT 30` in the lint queries
(`lint_graph.py:48,81,123`), not the backlog.

### Cause 1 — the AI-adjudication review pipeline is broken in the middle

134 findings (`orphan_entity` 50, `missing_org` 50, `ownerless_action` 26, and the 8
that route to `review_org`) plus 44 stalled `org_merge_review` pairs are on a path
whose two ends are wired correctly and whose middle is not:

- `daemon._run_review` (`daemon.py:1789`) builds review units, stashes them in
  `_pending_blocks` — **works**
- `prepare.attach_extra_blocks` merges them into the batch dict — **works**
- `prepare.write_units` (`prepare.py:726`) only emits unit files for keys in
  `enrich_blocks.UNIT_BLOCKS` (`merge_review` + `ANSWER_BLOCKS`). The five review
  families are absent, so the blocks are **silently dropped and never become work
  units** — **broken**
- `brain_enrich_push` (`mcp_server.py:685,705,1352`) only forwards
  `ANSWER_BLOCKS`, and its hand-written `inputSchema` (`:1167`) does not declare the
  review fields. Even a hand-fed subagent's verdicts would be discarded — **broken**
- `drain.BLOCK_DRAINERS` (`drain.py:146-171`) and the appliers in `review_apply.py`
  are complete — **works, never invoked**
- The subagent verdict rules (`plugin/agents/enrich-batch.md:295-410`) are written and
  correct — **works, never reached**

Evidence from the live daemon: the stash is re-attached every ~5 minutes for days —
`extra blocks attached: {'review_orphan': 50, 'review_missing_org': 50,
'review_ownerless': 26, 'review_org': 8, 'org_merge_review': 44}` — with **zero**
`review_*_drained` lines in the entire 45 MB log, while `profile_audit` (which *is* in
`UNIT_BLOCKS`) drains normally. The stash only clears on drain, so it is pinned in
daemon memory permanently.

The root cause is three hand-maintained block lists that drifted. The comment at
`enrich_blocks.py:9` states the drift as if it were a decision: *"drain.BLOCK_DRAINERS
(review_*/org_merge_review) is a SEPARATE registry and is intentionally not derived
from here."*

### Cause 2 — `lint:possible_duplicate` rows are stranded

`check_possible_duplicates` was deleted (`lint_graph.py:11`: *"redundant — deletion
scheduled Task A5"*). `resolve_findings_not_in` only runs for types the module still
produces, so nothing ever closes the 50 rows recorded on 2026-06-15. Every install
with history carries the same stranding.

### Cause 3 — `memory_promotion` is write-only

`memory_distil.py:107` records the finding when the distiller judges a session note
durable enough to become a real `memory/*.md` file. Nothing in the codebase reads or
resolves that type. The weekly gardener already owns promotion as job #3 of its
hygiene pass but cannot see the queue and has no way to close a finding.

## Scope

In scope: the three causes above.

Out of scope: the `LIMIT 50` / `LIMIT 30` lint caps. Once adjudication runs,
`review_max_apply_per_run` throttles applies anyway; the caps mainly make the
dashboard under-report (404 orphans reading as 50). Tracked separately.

## Division of labour

Two engines, split by the shape of the work:

- **Lint/graph families → the hourly Haiku enrich drainer pool.** High volume (404
  orphans, 242 ownerless actions), mechanical, and the appliers plus subagent rules
  already exist. Fixing the wiring is the whole job.
- **`memory_promotion` → the weekly gardener.** Low volume, needs judgment against
  the records repo, and the gardener already owns that repo.

## Component A — unify the block registry

`mcpbrain/enrich_blocks.py` becomes the single source of truth:

```python
ANSWER_BLOCKS = ("synthesis", "profile_synthesis", "community_synthesis",
                 "memory_distil", "profile_audit")

# Review/curator work: the daemon's review + curator cadences produce these, the
# subagent answers under the SAME key, drain.BLOCK_DRAINERS applies them.
REVIEW_BLOCKS = ("review_orphan", "review_missing_org", "review_ownerless",
                 "review_org", "org_merge_review")

UNIT_BLOCKS = ("merge_review", *ANSWER_BLOCKS, *REVIEW_BLOCKS)  # producer emits
PUSH_BLOCKS = (*ANSWER_BLOCKS, *REVIEW_BLOCKS)                  # push accepts
```

Consequent changes:

- **`prepare.write_units`** — no code change. It iterates `UNIT_BLOCKS`, so review
  units start being written. Unit ids are content hashes, so re-attaching the same
  stash each cycle stays idempotent.
- **`mcp_server.py`** — replace `_ENRICH_ANSWER_BLOCKS` with `PUSH_BLOCKS` at the
  three use sites (`:685` has-block-answer guard, `:705` inbox payload, `:1352`
  argument fan-out), and generate the `brain_enrich_push` `inputSchema` properties
  from `PUSH_BLOCKS` in a loop instead of hand-listing six entries. `merge_answers`
  stays hand-listed: it is the one block whose answer key differs from its unit key
  (`merge_review`). Update the tool description to match.
- **`drain.BLOCK_DRAINERS`** — stays in `drain.py` (drainers need that module's
  imports). The false comment at `enrich_blocks.py:9` is replaced by a statement of
  the real invariant.

### Invariant and tests

The guard that would have caught this:

- `set(BLOCK_DRAINERS) ⊆ set(PUSH_BLOCKS)` — a registered drainer whose key `push`
  refuses can never fire. One-directional, because `synthesis` is in `ANSWER_BLOCKS`
  but is drained by `synthesise_threads` rather than through `BLOCK_DRAINERS`.
- Every `REVIEW_BLOCKS` entry has a `BLOCK_DRAINERS` drainer.

The consistency test must import the modules that self-register drainers
(`profile_synth`, `community_synth`, `memory_distil`, `profile_audit`) so the registry
is fully populated, matching what `daemon.py:55-58` does at startup.

End-to-end test: stash a review block → `prepare_units` → assert a unit file with
`block == "review_orphan"` exists → `brain_enrich_push` a verdict → drain → assert the
finding is resolved and the entity mutated.

### Expected effect on the live store

The next cycle writes the stashed 134 review items and 44 `org_merge_review` pairs as
units. The hourly Haiku pool adjudicates them. `review_max_apply_per_run` (default 50)
throttles applies per pass, so the backlog clears over several passes rather than in
one shot. `_pending_blocks` clears once drain reports `<key>_drained`.

## Component B1 — retire the dead `possible_duplicate` rows

Add an explicit retirement list to `lint_graph.py`:

```python
# Finding types this module used to produce. A retired check leaves its rows
# stranded open forever (resolve_findings_not_in only runs for LIVE types), so
# each lint run closes them out.
RETIRED_FINDING_TYPES = ("lint:possible_duplicate",)
```

`run()` calls `store.resolve_findings_not_in(t, [], now)` for each entry, which closes
every open row of that type.

Explicit rather than a "close any type with no producer" sweep: that heuristic would
wrongly close `memory_promotion` and `org_unrecognised`, which are legitimately
produced by `memory_distil` and `drain` respectively. Rows are marked resolved, not
deleted, so history survives and the change is reversible. It self-heals on every
install at the next lint run, with no manual SQL.

## Component B2 — `memory_promotion` becomes a queue the gardener works

### New MCP tool: `brain_finding_resolve`

`brain_finding_resolve(finding_id: int, outcome: str, note: str = "") -> dict`

- Loads the finding via `store.get_finding`. Unknown or already-resolved returns
  `{"resolved": False, "error": ...}`.
- **Scoped**: rejects any finding whose type is not in `MANUAL_RESOLVE_TYPES =
  ("memory_promotion",)`, a module-level constant in `mcp_server.py` beside the tool,
  with an error naming why. Closing a
  `lint:*` finding by hand is churn — `resolve_findings_not_in` re-opens it on the
  next lint run because the underlying entity is still there — and the scope stops a
  session quietly dismissing graph-hygiene work the appliers own. The existing
  dashboard route `/api/dashboard/findings/<id>/dismiss` remains the human override
  for any type.
- `outcome` must be one of `promoted` | `merged` | `dismissed`; anything else is
  rejected.
- On success: `store.record_change("finding_resolved", ref_id=str(finding_id),
  summary=…, detail=note, source="mcp")` for the audit trail, then
  `store.resolve_finding(finding_id)`.

### Gardener routine changes

`mcpbrain/routines/gardener.md` gains a promotion-queue step in the weekly hygiene
pass. The routine is served through `brain_routine` from `mcpbrain/routines/` and has
no `plugin/` mirror, so it is a single-file change.

Per finding:

1. `brain_proactive(finding_type="memory_promotion")`. Each finding's `ref_id` **is**
   the note's `doc_id`; `detail` carries the distiller's `reason=` and `target_hint=`.
2. `brain_read(ref_id)` for the note's full text.
3. Check it against the existing `memory/*.md` files and the `MEMORY.md` index.
4. Act on one of three outcomes:
   - **promote** — `brain_memory_write(slug, description, body, memory_type)`; the
     daemon authors `memory/<slug>.md` plus the `MEMORY.md` pointer and commits
   - **merge** — the fact already lives in an existing memory file; fold it in and fix
     the pointer (ordinary hygiene, already permitted)
   - **dismiss** — not durable; close it and touch nothing
5. `brain_finding_resolve(finding_id, outcome)` in **all three** cases.

Promotions and merges count against the routine's existing "max 10 memory file updates
per run"; dismissals touch no files and are uncapped. The current 16 clear over two
weekly runs.

The routine's standing rule against hand-authoring new memory files is preserved:
promotion goes through `brain_memory_write` → daemon, the same as every other
structured write.

### Known trade-off

`brain_memory_write` is queued, so the gardener resolves the finding roughly one
daemon cycle before the file physically lands. If that write failed, the finding would
already be closed. Accepted: the capture is durable on disk the moment the tool
returns, and a two-phase handshake does not fit a weekly routine.

## Testing

- Registry invariants (Component A), with self-registering drainer modules imported.
- `write_units` emits a unit per review family; push accepts and forwards review keys;
  end-to-end stash → unit → push → drain → finding resolved.
- `RETIRED_FINDING_TYPES` closes open rows of a retired type and leaves live types
  untouched.
- `brain_finding_resolve`: accepts `memory_promotion`; rejects `lint:orphan_entity`,
  an unknown id, an already-resolved finding, and a bad `outcome`; records the change
  and resolves on success.

Scope test runs to the edited modules and their direct dependents; the full suite is
the user's to run.

## Files touched

- `mcpbrain/enrich_blocks.py` — `REVIEW_BLOCKS`, `PUSH_BLOCKS`, corrected comment
- `mcpbrain/mcp_server.py` — `PUSH_BLOCKS` at three sites, generated push schema,
  `brain_finding_resolve` tool + registration
- `mcpbrain/lint_graph.py` — `RETIRED_FINDING_TYPES` + retirement sweep in `run()`
- `mcpbrain/routines/gardener.md` — promotion-queue step
- `tests/` — registry invariants, review-block round trip, retirement sweep,
  `brain_finding_resolve`
