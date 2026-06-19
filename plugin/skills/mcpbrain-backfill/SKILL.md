---
name: mcpbrain-backfill
description: Backfill enrichment of your full email history. Drains the work-unit queue with one fresh-context subagent per unit, nudging the daemon to refill, until the whole history is enriched.
---

# Backfill enrichment

Drains the entire mcpbrain enrichment **work queue** — your full email history — by
repeatedly taking the ready units and handing each to a fresh-context `enrich-batch`
subagent, then nudging the daemon to refill the queue. No context (not even yours,
the orchestrator) ever holds more than a unit id and a one-line status. Loops until
the queue stays empty.

MCP-only — it does not read the spool via shell. Load the tools first if needed:
`ToolSearch("select:mcp__mcpbrain__brain_enrich_units,mcp__mcpbrain__brain_enrich_advance")`.

## Loop

```
waves = 0; units_done = 0; empty = 0
WHILE empty < 3:
  ready = brain_enrich_units()
  IF ready.empty:
      empty += 1
      brain_enrich_advance()          # nudge the daemon to drain + refill the queue
      WAIT ~10s
      CONTINUE
  empty = 0; waves += 1

  # Fan out: one enrich-batch subagent per unit, ~5 in parallel at a time.
  # Dispatch each on model `haiku` — extraction follows the rules well on Haiku and
  # is far cheaper than Sonnet for a large backlog.
  FOR each unit IN ready.units:
      dispatch enrich-batch (Task tool, model haiku) with its unit_id
  collect each subagent's one-line status; add to units_done

  brain_enrich_advance()              # apply the pushed results + top the queue back up

REPORT: waves, units_done, final queue state
```

## Notes

- Each `enrich-batch` subagent is self-contained: it pulls its own unit
  (`brain_enrich_pull(unit_id=…)`, which carries the rules + context) and pushes its
  own result (`brain_enrich_push(unit_id=…)` → `enrich_inbox/<unit_id>.json`). The
  daemon applies it and deletes the unit.
- `brain_enrich_units` claims each unit it hands out with a short lease, so a unit in
  flight is never handed to two subagents — the loop won't double-process.
- `brain_enrich_advance` runs one daemon drain+prepare cycle: it applies pushed
  results (freeing those units) and produces the next units from still-un-enriched
  history. The queue is bounded, so the daemon refills it as units drain.
- Re-enriching a unit is idempotent (the daemon upserts), so a redelivered unit only
  wastes work; it never corrupts the graph.
- Stop early on `/stop`, or after three consecutive waves where every unit returns
  `ERROR:`.
```bash
mcpbrain home   # the store/queue live here, if you need to inspect logs/enrich.log
```
