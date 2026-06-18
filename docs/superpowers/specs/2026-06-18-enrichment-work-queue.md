# Enrichment work-queue refactor

Status: proposed (2026-06-18)
Supersedes the snapshot/manifest model added in 0.7.28–0.7.32.

## Problem

Today the daemon writes **one** `enrich_queue/pending.json` (≤100 threads + optional
blocks) and **rewrites it every cycle** (~5 min). The enrich consumer can't read a
moving target, so 0.7.28–0.7.32 bolted on machinery purely to survive the churn:

- `active/<batch_id>.json` snapshots (freeze a stable view)
- `batch_id` keying + TTL prune (stop overlapping runs clobbering the snapshot)
- a read-time `brain_enrich_manifest` that re-shards the frozen batch

So there are **two chunking steps** (prepare batches; manifest shards) and a snapshot
layer bridging them. Most of that exists only because `pending.json` churns.

## Model: a durable work queue

The daemon is a **producer** of immutable, pre-sized **work units**; the enrich
session is a trivial **consumer** — one subagent per unit, until the queue is dry.
Because units never change once written, there is nothing to freeze: no snapshot, no
batch_id, no prune, no manifest.

### Unit (`enrich_queue/units/<unit_id>.json`)

```json
{ "unit_id": "u-ab12cd34",
  "kind": "thread",                // or "block"
  "threads": [ <thread block>, ...] // kind=thread: the payloads, sized to fit the cap
}
```
```json
{ "unit_id": "u-ef56gh78",
  "kind": "block",
  "block": "merge_review",         // kind=block: one block type + its item slice
  "items": [ <item>, ... ] }
```

Units carry **only their work**. Rules and context are NOT persisted per unit (they
change, and would duplicate ~17KB across thousands of files) — `brain_enrich_pull`
attaches the *current* rules + context at read time.

Unit size: packed to the pull cap (`_PULL_MAX_CHARS`), exactly as threads/blocks are
sized today — maximises work per subagent, one subagent per unit.

### Lifecycle

```
produce ─▶ list ─▶ claim(lease) ─▶ pull(unit_id) ─▶ push(unit_id) ─▶ drain ─▶ delete
 daemon    MCP        MCP             subagent          subagent       daemon    daemon
```

- **produce** — `prepare()` tops the queue up to a bounded window (e.g. ≤200 ready
  units) from un-enriched threads + due blocks. Backpressure: it refills as units
  drain, so a 60K backlog doesn't write 60K files at once.
- **list** — `brain_enrich_units()` returns ready unit *descriptors* (`unit_id`,
  `kind`, `block`, counts) — **no payloads**, so the orchestrator stays context-flat.
  Skips units with a fresh claim. Stamps a claim (lease ~15 min) on the ones returned.
- **pull** — `brain_enrich_pull(unit_id)` returns that unit's payload + current
  `rules` + `context`.
- **push** — `brain_enrich_push(unit_id, …)` writes `enrich_inbox/<unit_id>.json`.
- **drain** — daemon applies the result, marks chunks enriched, **deletes the unit +
  its claim**, stamps `logs/enrich.log`.
- Crash recovery: an expired claim (lease elapsed, no result) makes the unit
  re-listable. Re-applying a unit is idempotent (drain upserts), so redelivery is safe.

### Claim (lease)

`enrich_queue/claims/<unit_id>` marker file; mtime = lease start. Keeps units
immutable (the lease lives beside the unit, not in it). `brain_enrich_units` skips a
unit whose claim mtime is within the lease TTL; otherwise (re)claims it. This is what
lets `backfill` loop quickly without re-handing-out in-flight units — the role the
snapshot/batch_id played, but per-unit and without freezing.

## MCP surface

| before | after |
|---|---|
| `brain_enrich_manifest()` → shards | `brain_enrich_units()` → claimed unit descriptors |
| `brain_enrich_pull(thread_ids/block/…, batch_id)` | `brain_enrich_pull(unit_id)` |
| `brain_enrich_push(batch_id, shard, …)` | `brain_enrich_push(unit_id, …)` |
| `brain_enrich_advance()` | unchanged (nudge produce+drain) |

Consumer routine (`routines/enrich.md`) and `agents/enrich-batch.md` collapse to:
list units → one subagent per `unit_id` → each pulls by `unit_id`, extracts, pushes
by `unit_id` → loop while units remain. Same shape for hourly and backfill (backfill
just keeps looping + `advance`).

## Producer (prepare) changes

`prepare()` stops writing `pending.json`. Instead it:
1. computes the chunking it already computes (thread slices + block-item slices), and
2. writes each slice as a unit file **iff** the queue is below the window cap, skipping
   threads/blocks already represented by an undrained unit (dedupe by content key).

Block cadences (resolution_due, synthesis, etc.) gate which block units get produced,
as today.

## Drain changes

`drain()` already applies `enrich_inbox/*.json` by content. Add: on a successful
apply, delete `units/<unit_id>.json` + `claims/<unit_id>`. Remove the
`pending.json`-deletion branch (no pending.json). `enrich.log` stamp stays.

## What gets deleted

`_enrich_snapshot_*`, `_resolve_snapshot`, `_prune_snapshots`, `_safe_batch_token`,
`make_brain_enrich_manifest`, the snapshot/batch_id/block_start params on pull, and
the `active/` dir. Net: the diff removes more than it adds.

## Migration / rollout

- Additive first: ship `units` producer + `brain_enrich_units`/unit-`pull`/unit-`push`
  alongside the existing path; switch the routine to the unit path; then delete the
  snapshot/manifest path in a follow-up once a live run confirms the unit path drains.
- One-time: on upgrade, an existing `pending.json` is ignored (the producer just
  starts emitting units); no data migration (units are derived from the store's
  un-enriched chunks, the source of truth).

## Risks

- Touches the core live pipeline (prepare, drain, MCP tools, routines). Mitigate with
  the additive-then-delete rollout and the full test suite.
- Many small unit files for a large backlog — bounded by the producer window + delete-
  on-drain. Claims dir similarly bounded.
- Lease tuning: too short re-hands-out slow units; too long delays retry of crashed
  ones. 15 min default, configurable.

## Open decisions

1. **Lease/claim**: include from day one (needed for the backfill loop) vs defer and
   accept idempotent duplication initially. Recommendation: include — it's small and
   it's the thing that makes the loop correct.
2. **Window size** (ready-unit cap) and **lease TTL** defaults.
