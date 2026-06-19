# Brain enrichment (hourly) — work queue

Enrich the pending work units through the mcpbrain MCP tools. You are the
**orchestrator**: you hand each unit to a subagent, so your own context never holds
the email bodies — only unit IDs and one-line status replies. Self-contained —
needs no skill or command file.

1. Call **`brain_enrich_units`**. If it returns `{"empty": true}`, stop and report
   `DONE: queue empty`.
2. Otherwise it returns `units` — a list of `{unit_id, kind, block, count}`. Each is
   one unit of work (a slice of threads, or one block type's items).
3. For **each unit**, spawn the **`enrich-batch`** subagent (the Task tool,
   `subagent_type: enrich-batch`). That agent runs on Haiku (set in its own
   frontmatter) and carries the FULL extraction protocol in its system prompt — so
   the rules sit in one cacheable prefix shared across the whole fan-out, never in
   your context. Spawn them in parallel — up to ~5 Task calls in one message, then
   the next wave. Give each subagent EXACTLY this one-line instruction, substituting
   the unit's `unit_id` (the agent already knows the protocol — do not repeat it):

   > Enrich unit `<unit_id>`. Act autonomously; do not ask questions.

4. When the wave's subagents have replied, call **`brain_enrich_units`** again for the
   next wave. Repeat until it returns `{"empty": true}` **or you have run 10 waves**,
   whichever comes first — stop at 10 even if units remain; the next hourly run (or
   the backfill skill) continues the rest. This caps a single run's time and cost.
5. Report: `DONE: <N> units across <W> waves` (note if you stopped at the 10-wave cap).

Never pull unit payloads into your own context — each subagent pulls its own unit.
Use the MCP tools only; do not read skill/command files or shell into the spool.
