# Brain enrichment — work queue

Drain the pending enrichment work units through the mcpbrain MCP tools. You are the
**orchestrator**: you hand each unit to a fresh subagent, so your own context never
holds email bodies — only unit ids. You keep NO per-unit state. Self-contained —
needs no skill or command file.

**Models:** you (the coordinator) run on **Sonnet** — the scheduled task runs in
**Auto permission mode**, which Claude Code only offers on Sonnet, so a Haiku
coordinator would stall on prompts. Every `enrich-batch` subagent runs on **Haiku**,
set **explicitly per dispatch** (the agent frontmatter is not always honored); that
is where the volume and the cost savings live.

## Loop

1. Call **`brain_enrich_units`**. If it returns `{"empty": true}`, stop and report
   `DONE: queue empty`.
2. Otherwise it returns `units` — a list of `{unit_id, kind, block, count}`. For
   **each unit**, spawn the **`enrich-batch`** subagent (Task tool,
   `subagent_type: enrich-batch`, **`model: haiku`** set explicitly). Fan out up to
   ~12 in parallel per message. Give each subagent EXACTLY this one line, with the
   unit's `unit_id` substituted (the agent already carries the extraction protocol —
   do not repeat it):

   > Enrich unit `<unit_id>`. Act autonomously; do not ask questions.

3. When the wave's subagents return, **do not parse their replies**. Call
   **`brain_enrich_advance`** — the daemon drains every pushed result, applies it,
   and deletes that unit from the queue.
4. Go back to step 1. A unit that was enriched is gone from the queue; a unit that
   was NOT (its subagent derailed, or is still running under its lease) simply
   re-appears on a later list once its 15-minute claim lease expires, and you
   dispatch it again. Done-ness is queue state, never reply text.
5. Stop when `brain_enrich_units` returns `{"empty": true}`. There is no wave cap
   and no subagent limit — if this session runs out of subagent capacity before the
   queue empties, that is fine: report what remains and the next run (or a re-run)
   continues. **Backfill is just re-running this routine.**
6. Report: `DONE: queue empty` or `PARTIAL: units still pending — re-run to continue`.

Never pull unit payloads into your own context — each subagent pulls its own unit
(`brain_enrich_pull(unit_id=…, with_rules=false)`) and pushes its own result
(`brain_enrich_push(unit_id=…)`). Use the MCP tools only; do not read skill/command
files or shell into the queue.
