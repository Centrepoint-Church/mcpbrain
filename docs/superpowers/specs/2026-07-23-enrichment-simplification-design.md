# Enrichment system simplification — design

**Date:** 2026-07-23
**Status:** approved (design), pending implementation plan
**Scope:** the enrichment producer/consumer, the 4 `brain_enrich_*` tools, the
orchestration prompt(s), and the drain/apply path. NOT the daemon loop, sync,
embedding, or retrieval.

## Motivation

A full review of the enrichment stack found the core engine sound (crash-safe
apply-before-mark ordering, content-addressed idempotent units, lease-based
claims, backpressure, give-up caps) but the *edges* accreted overcomplication and
a half-finished migration from an older single-file queue. Nine issues, grouped
below. The orchestration rework (A) is driven by one measured fact: **a coordinator
session's ~200-subagent ceiling is the real throughput limit**, so the wave-count
caps and the second "backfill" entry point were never the binding constraint.

Two behaviours change; everything else is behaviour-preserving:
- **#1** reduces token cost (rules no longer double-sent to workers).
- **#7** makes a config value take effect on next cycle instead of only on restart.

---

## A. Orchestration merge (findings #4, #8, #9)

### Problem
`mcpbrain/routines/enrich.md` (the hourly coordinator) and
`plugin/skills/mcpbrain-backfill/SKILL.md` (backfill) are ~90% identical prose:
same model split, same fan-out loop, same requeue guard. They differ only in stop
condition (hourly caps at "15 waves"; backfill loops "until dry"). Both decide a
unit is done by **string-matching the worker's reply** against
`unit <id>: <n> <kind>` / `unit <id>: gone`. This is:
- brittle (a coordinator/worker/tool three-way prose contract), and
- the reason the two files drift (the live 15-vs-10 wave doc bug).

### Measured constraint
`brain_enrich_units` hands out up to `_UNITS_BATCH_DEFAULT = 30` units/call = 30
subagents/wave. The 15-wave cap implies up to 450 subagents, but a session tops out
around **200 subagents**, reached at wave ~7. So:
- the 15-wave cap is unreachable and effectively dead;
- backfill's "until dry" is *also* bounded by the same ~200 ceiling — within one
  session hourly and backfill enrich the same ~200 units and stop for the same
  reason. Backfill's only real distinction was "meant to be re-run."

### Design
**One loop, queue-driven, budget-bounded.** The canonical loop lives in
`mcpbrain/routines/enrich.md` (served to the hourly scheduled task via the
`brain_routine` tool). It is the *only* enrichment orchestration prompt.

- **Done-ness = queue state, not reply text.** A successfully pushed unit is
  drained and its unit file deleted, so it stops appearing from
  `brain_enrich_units`. The coordinator no longer parses worker replies at all.
- **Retry by re-listing.** After dispatching a wave, call `brain_enrich_advance`
  (drains pushed results → deletes those units), then call `brain_enrich_units`
  again. Anything still returned is not-yet-done (never pushed, or lease expired)
  → re-dispatch. The coordinator keeps a per-`unit_id` **dispatch counter** and
  gives up on a unit after **K = 3** dispatches (counting IDs — no prose parsing).
- **Single terminator: queue empty OR subagent budget spent.** Stop when
  `brain_enrich_units` returns `{"empty": true}` (natural end) or the coordinator
  has dispatched ~**180** subagents (headroom under the ~200 ceiling for retries).
  The wave-count concept is removed entirely.
- **Backfill = re-run the routine.** For a backlog larger than one session, run the
  routine again (or let the next hourly run continue at ~200 units/hour). The
  routine's final report states how many units remain pending so the operator knows
  whether to re-run.

### `brain_enrich_push` validation stays
The push schema checks (reject non-list / empty `extractions` with no block answer)
are the **store-write safety boundary**, not a duplicate of the deleted reply
match. They remain unchanged — they are the only thing preventing a derailed
subagent from silently draining a unit with zero extractions.

### Files
- **Rewrite** `mcpbrain/routines/enrich.md` → single budget-bounded, queue-driven loop.
- **Delete** `plugin/skills/mcpbrain-backfill/` entirely.
- **Update** references to the deleted skill: `plugin/agents/enrich-batch.md:9`
  ("the hourly enrich routine and the backfill skill" → "the enrich routine"),
  `plugin/commands/install.md` (any backfill-skill mention), `docs/ARCHITECTURE.md`.
  Historical `docs/superpowers/plans/*` are dated records — left untouched.

---

## B. Mechanical cleanups

### #1 — `with_rules` is a dead parameter (double rules payload)
`brain_enrich_pull` accepts `with_rules` but its `inputSchema` (mcp_server.py:1071)
declares only `unit_id`, and the dispatch (mcp_server.py:1240) never forwards it —
so `with_rules` is permanently `True`. Workers already carry the ~11 KB rules in
their cacheable system prompt, so every pull re-sends the rules **uncached**: paid
for twice per worker.

**Fix:** add `with_rules` (boolean, default `true`) to the tool `inputSchema`, and
forward `arguments.get("with_rules", True)` in the dispatch. `enrich-batch` workers
call with `with_rules=false` (already in their prompt) → no double payload;
general-purpose callers keep the default `true` and stay self-contained. The
rules-sync apparatus (`bin/sync_agents.py`, `test_enrich_agent_rules_in_sync`) is
retained — it is what makes `with_rules=false` safe, and now actually earns its keep.

### #2 — Delete the legacy `pending.json` / `batch_id` queue
The system migrated from a single `pending.json` (keyed `batch_id`) to the per-unit
queue (`units/<uid>.json`, content-hashed `unit_id`). The old design was never removed:
- `prepare.prepare()` + `prepare._write_pending()` (labelled test-harness-only)
- `bin/drain_backlog.py`
- `mcpbrain/maintenance/extractor_io.py` (imported only by `drain_backlog.py`)
- the legacy-format branch handling in `drain()`

**Fix:** delete all four. Redirect the tests that exercised `prepare()` /
`_write_pending` / `drain_backlog` onto `prepare_units()` / `build_pending()` (they
were testing noise-filter / merge-review / build_pending assembly, which survive).
`tests/test_package_data.py`'s `extractor_io` reference and any
`test_integration_spool` / `test_prepare` / `test_daemon_p3` usage of the legacy
path are updated. `mcpbrain/maintenance/*` is dev-only (excluded from the wheel), so
deletion is release-safe. Keep the `enrich_mode="spool"` config *value* for
backward compat; only remove the misleading `spool | gemini | off` comment
(daemon.py:490) and add a one-line "spool = the unit queue" note.

### #3 — One shared message-identity rule (the Drive-bug class)
Two functions legitimately do *different* jobs but must agree on message identity:
- `thread_enrich.reassemble_thread` emits a `message_id` per message.
- `store.doc_ids_for_messages` must resolve that id back to the same chunks.
The 0.7.98 Drive bug (~85k chunks never enriched) was these two disagreeing.

**Fix:** extract `message_identity(meta, doc_id)` = `file_id → message_id → doc_id`
into a single helper used by **both** `reassemble_thread` and
`doc_ids_for_messages`. `thread_enrich._group_key` (thread-level batching) calls it
and prepends `thread_id`. This makes drift impossible without conflating the two
distinct jobs (batching vs per-message identity) — they are NOT collapsed into one
function.

### #5 — Deduplicate the drain give-up logic
`drain.py` has two near-identical bump-and-give-up blocks (`:368-381` invalid
extraction; `:431-442` matched-no-chunk). **Fix:** extract
`_give_up_or_bump(store, doc_ids, summary)` (bump `enrich_attempts`; at
`>= _EMPTY_ATTEMPT_CAP` mark_enriched + `summary["gave_up"] += 1`) and call it from
both sites.

### #6 — Single source for the block-type set
`_ENRICH_ANSWER_BLOCKS` (mcp_server, 5 keys), `_UNIT_BLOCKS` (prepare, 6 keys incl.
`merge_review`), and `BLOCK_DRAINERS` (drain) are hand-kept and can drift.
**Fix:** define the block-type set once (a single module-level structure — e.g. a
`BLOCK_TYPES` tuple, or a small dataclass carrying which list each belongs to) and
derive the three consumers from it. Adding a block type becomes a one-line change.
The exact home for the shared definition is an implementation-plan detail; the
constraint is: no more than one place to edit.

### #7 — Size caps read config at call time
`prepare._UNIT_PULL_CAP` and `mcp_server._PULL_MAX_CHARS` both read
`config.unit_pull_cap()` **at import**, freezing the value until daemon restart.
**Fix:** read `config.unit_pull_cap()` at call time (via a small accessor) so a
config change takes effect on the next cycle. Keep the "stay in lockstep" invariant
by having both derive from the same accessor.

### #8 — Docs / stale comments
Subsumed by A (the wave-count doc bug disappears with the wave concept) plus: remove
the stale `spool | gemini | off` daemon comment (#2). The triple
`_reassemble_thread` call in `prepare.py` (`_filter_noise`, `_apply_trivial_threads`,
`_thread_block`) is **left as-is** — it is cheap correctness insurance, not worth a
caching layer (YAGNI).

---

## Testing strategy

TDD per change. Tests are scoped to edited + directly-impacted files (the operator
runs the full suite separately). Specifically:
- **A:** remove/replace tests asserting the reply string contract
  (`test_plugin_assets`, `test_mcp_enrich_meeting_tools` as applicable); add a test
  that the routine loop is documented as queue-driven + budget-bounded; assert the
  backfill skill directory is gone (mirrors the existing `test_no_toplevel_bin_dir`
  guard style).
- **#1:** test that `brain_enrich_pull(with_rules=false)` omits `rules` and
  `with_rules=true`/default includes them; assert the tool schema declares
  `with_rules`.
- **#2:** redirect legacy-path tests onto `prepare_units`/`build_pending`; assert
  `drain()` still handles current unit files; remove `drain_backlog`/`extractor_io`
  tests.
- **#3:** unit tests for `message_identity` covering Drive (`file_id`), email
  (`message_id`), and fallback (`doc_id`); a round-trip test that a reassembled
  Drive doc's emitted id resolves via `doc_ids_for_messages` to all its chunks.
- **#5/#6/#7:** behaviour-preserving refactors — existing drain / block / cap tests
  must stay green; add a test that changing `unit_pull_cap` config is observed
  without re-import (#7).

## Out of scope
- Daemon loop, sync, embedding, retrieval, org-baseline, Windows install.
- Renaming the `enrich_mode="spool"` config value (backward-compat risk, no gain).
- The ~200 subagent ceiling itself (harness constraint, not ours to change).

## Risks
- Deleting `prepare()` may strand test helpers (`tests/helpers/stub_extractor.py`,
  `tests/e2e/test_full_loop.py`) — audit and redirect in the plan.
- Removing the backfill skill is a user-visible plugin change; it ships via the
  marketplace mirror, so the release must sync `plugin/` and note the removal.
- #6's shared block definition must preserve `merge_review` being a `_UNIT_BLOCKS`
  member but not an `_ENRICH_ANSWER_BLOCKS` member — the derivation must encode that
  asymmetry, not flatten it.
