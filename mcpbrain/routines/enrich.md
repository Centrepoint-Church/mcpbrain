# Brain enrichment (hourly) — work queue

Enrich the pending work units through the mcpbrain MCP tools. You are the
**orchestrator**: you hand each unit to a subagent, so your own context never holds
the email bodies — only unit IDs and one-line status replies. Self-contained —
needs no skill or command file.

1. Call **`brain_enrich_units`**. If it returns `{"empty": true}`, stop and report
   `DONE: queue empty`.
2. Otherwise it returns `units` — a list of `{unit_id, kind, block, count}`. Each is
   one unit of work (a slice of threads, or one block type's items).
3. For **each unit**, spawn a **subagent** (the Task tool, general-purpose). Spawn
   them in parallel — up to ~5 Task calls in one message, then the next wave. Give
   each subagent EXACTLY this instruction, substituting the unit's `unit_id`:

   > Automated enrichment of one unit — act autonomously, do not ask questions.
   > 1. Load the tools:
   >    `ToolSearch("select:mcp__mcpbrain__brain_enrich_pull,mcp__mcpbrain__brain_enrich_push")`.
   > 2. Call `brain_enrich_pull` with `unit_id=<unit_id>`. If it returns
   >    `{"empty": true}`, reply `unit <unit_id>: gone` and stop.
   > 3. The result has a **`rules`** field — the FULL extraction protocol — plus the
   >    `context`. Follow `rules` EXACTLY:
   >    - `kind` `"thread"`: produce one extraction object per thread in `threads`.
   >    - `kind` `"block"`: answer the block named in `block` for each item in
   >      `items` (`merge_review` → `merge_answers`; otherwise the field of the same
   >      name: `synthesis` / `profile_synthesis` / `community_synthesis` /
   >      `memory_distil` / `profile_audit`).
   > 4. Call `brain_enrich_push` with `unit_id=<unit_id>`, `extractions=[…]` (and the
   >    block answer field if this is a block unit). Confirm `{"written": true}`.
   > 5. Reply with ONE line only: `unit <unit_id>: <n> <kind>`. Nothing else.

4. When the wave's subagents have replied, call **`brain_enrich_units`** again for the
   next wave. Repeat until it returns `{"empty": true}`.
5. Report: `DONE: <N> units across <W> waves`.

Never pull unit payloads into your own context — each subagent pulls its own unit.
Use the MCP tools only; do not read skill/command files or shell into the spool.
