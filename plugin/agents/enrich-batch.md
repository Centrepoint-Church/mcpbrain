# enrich-batch

Per-unit enrichment subagent (the hourly enrich task and the backfill skill). You
are handed one `unit_id`. You pull that unit, extract it, and push the result —
nothing else, so the orchestrator's context stays flat no matter how large the
history is.

## Protocol

1. Load the tools:
   `ToolSearch("select:mcp__mcpbrain__brain_enrich_pull,mcp__mcpbrain__brain_enrich_push")`.
2. Call `brain_enrich_pull` with `unit_id=<your unit_id>`. If it returns
   `{"empty": true}`, return exactly `unit <unit_id>: gone`.
3. The result carries a **`rules`** field — the FULL extraction protocol (envelope
   schema, entity/relation/merge rules) — plus `context`. Follow `rules` EXACTLY:
   - `kind` `"thread"`: produce one extraction object per thread in `threads`.
   - `kind` `"block"`: answer the block named in `block` for each item in `items`
     (`merge_review` → `merge_answers`; otherwise the field of the same name:
     `synthesis` / `profile_synthesis` / `community_synthesis` / `memory_distil` /
     `profile_audit`).
4. Call `brain_enrich_push` with `unit_id=<your unit_id>`, `extractions=[…]` (and the
   block answer field if this is a block unit). Confirm `{"written": true}`.
5. Return ONE line only: `unit <unit_id>: <n> <kind>`, or `ERROR: <reason>`.

Use the MCP tools only. Do not read the spool via shell, and do not read skill or
command files — everything you need (the unit's work + rules + context) is in the
pull response.
