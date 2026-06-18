---
name: mcpbrain-backfill
description: Backfill enrichment of your full email history. Fans each pending batch out across per-shard subagents (fresh context each) and loops until the spool is dry, so a large history never hits limits.
---

# Backfill enrichment

Drains the entire mcpbrain enrichment spool — your full email history — by repeatedly
fanning each pending batch out across per-shard `enrich-batch` subagents, then nudging
the daemon to prepare the next batch. Each shard runs in a fresh-context subagent, so
no context (not even yours, the orchestrator) ever holds more than thread IDs and
one-line status replies. Loops until the spool is dry.

This is MCP-only — it does not read the spool via shell. The tools live on the
mcpbrain MCP server; load them first if needed:
`ToolSearch("select:mcp__mcpbrain__brain_enrich_manifest,mcp__mcpbrain__brain_enrich_advance")`.

## Loop

```
rounds = 0; empty = 0; total_threads = 0
WHILE empty < 2:
  plan = brain_enrich_manifest()
  IF plan.empty:
      empty += 1
      brain_enrich_advance()        # nudge in case a batch is mid-prepare
      WAIT ~10s
      CONTINUE
  empty = 0; rounds += 1
  prev_batch = plan.batch_id

  # Fan out: one enrich-batch subagent per shard, ~5 in parallel at a time.
  FOR each shard IN plan.shards:
      dispatch enrich-batch  (Task tool) with:
          batch_id    = plan.batch_id
          thread_ids  = shard.thread_ids
          with_blocks = shard.with_blocks
          shard       = shard.shard
  collect each subagent's one-line status; add thread counts to total_threads

  # Advance to the next batch: drain the pushed shards + prepare the next pending.
  REPEAT up to ~6 times:
      brain_enrich_advance()        # immediate drain -> prepare cycle
      WAIT ~10s
      IF brain_enrich_manifest().batch_id != prev_batch OR .empty: BREAK

REPORT: rounds, total_threads enriched, final spool state
```

## Notes

- Each `enrich-batch` subagent is self-contained: it pulls its own `thread_ids`
  (which carry the extraction `rules`) and pushes its own `shard` —
  `enrich_inbox/<batch>.<shard>.json` — so parallel shards never clobber each other.
- `brain_enrich_advance` runs one daemon drain+prepare cycle. Because the daemon
  prepares *before* it drains, the next batch may take two nudges to appear — that's
  why the advance step polls the manifest's `batch_id` rather than nudging once.
- Re-enriching a thread is idempotent (the daemon upserts), so a duplicated shard
  only wastes work; it never corrupts the graph.
- Stop early on `/stop`, or after three consecutive rounds where every shard returns
  `ERROR:`.
```bash
mcpbrain home   # the store/spool live here, if you need to inspect logs/enrich.log
```
