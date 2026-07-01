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

## Models (same split as the hourly enrichment cycle)

- **Coordinator (you): Sonnet.** Keeps the split identical to the hourly task, which must
  run the coordinator on Sonnet: the scheduled task runs in **Auto permission mode** and
  Claude Code scheduled tasks only offer Auto mode on Sonnet (a Haiku coordinator stalls on
  permission prompts). The coordinator's work is mechanical and cheap regardless — fan out one
  subagent per unit; the requeue guard is a literal check, not a judgement: a unit is done IFF
  its reply matches `unit <unit_id>: <n> <kind>` or `unit <unit_id>: gone`.
- **Subagents: Haiku.** Dispatch every `enrich-batch` subagent — first try AND retries
  — with **`model: haiku` set EXPLICITLY in the Task call**. The agent frontmatter is
  not always honored, and extraction follows the rules well on Haiku at a fraction of
  Sonnet's cost over a large backlog.

`brain_enrich_units` hands out at most a wave's worth of units per call (capped, and it
claims only those), so this loop and the hourly cycle can both pull from the queue at
once without one starving the other — keep calling it for the next wave.

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

  # Fan out: one enrich-batch subagent per unit, ~12 in parallel at a time.
  # Dispatch with model: haiku EXPLICITLY (the agent frontmatter is not always
  # honored). The agent carries the extraction rules in its system prompt, so the
  # rules sit in one cacheable prefix shared across the fan-out — and Haiku is far
  # cheaper than Sonnet for a large backlog.
  FOR each unit IN ready.units:
      dispatch enrich-batch (Task tool, subagent_type: enrich-batch, model: haiku) with its unit_id

  # Requeue guard: a unit is done ONLY if its subagent replied with a clean
  # `unit <unit_id>: <n> <kind>` (or `… : gone`) line. Any other reply — narration,
  # raw JSON, "saved to …", ERROR:, or silence — means it derailed and did NOT push.
  # Re-dispatch that same unit_id to a fresh subagent (pull-by-id still works); retry
  # a derailed unit at most twice, then leave it for a later run. Count only clean
  # replies toward units_done.
  FOR each derailed unit (reply not a clean status line), up to 2 retries:
      dispatch enrich-batch (Task tool, subagent_type: enrich-batch, model: haiku) with its unit_id

  brain_enrich_advance()              # apply the pushed results + top the queue back up

REPORT: waves, units_done, final queue state
```

## Notes

- Each `enrich-batch` subagent is self-contained: it carries the extraction rules in
  its own system prompt, pulls its unit (`brain_enrich_pull(unit_id=…, with_rules=false)`
  → context + work), and pushes its result (`brain_enrich_push(unit_id=…)` →
  `enrich_inbox/<unit_id>.json`). The daemon applies it and deletes the unit. A unit
  with no successful push is never deleted, so the requeue guard above can safely
  re-dispatch it.
- `brain_enrich_units` returns at most a capped wave of units and claims each one it
  hands out with a short lease, so a unit in flight is never handed to two subagents —
  the loop won't double-process, and call it again for the next wave.
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
