# Brain enrichment — work queue

Drain the pending enrichment work units through the mcpbrain MCP tools. You are the
**orchestrator**: you spawn a small pool of drainer subagents that self-serve units
from the queue, so your own context never holds email bodies — only counts. You keep
NO per-unit state. Self-contained — needs no skill or command file.

**Models:** you (the coordinator) run on **Sonnet** — the scheduled task runs in
**Auto permission mode**, which Claude Code only offers on Sonnet, so a Haiku
coordinator would stall on prompts. Every `enrich-batch` drainer runs on **Haiku**,
set **explicitly per dispatch** (the agent frontmatter is not always honored); that is
where the volume and the cost savings live.

## Loop

1. Call **`brain_enrich_pending`**. If it returns `{"pending": 0}`, stop and report
   `DONE: queue empty`. Otherwise note the count.
2. Spawn a **pool of 10 `enrich-batch` drainers** in a single message (Task tool,
   `subagent_type: enrich-batch`, **`model: haiku`** set explicitly on each). Give each
   drainer EXACTLY this one line (the agent already carries the drain protocol and the
   extraction rules — do not repeat them):

   > Drain up to 5 enrichment units: loop claim → extract → push until an empty claim or 5 done. Act autonomously; do not ask questions.

   The pool size (10 drainers) and the per-drainer cap (5 units) are set here in the
   prompt — adjust these numbers to trade throughput against a drainer's context growth.

   Each drainer claims its own units via `brain_enrich_claim`, so you never hand out
   unit ids and never pull payloads into your own context.
3. When the wave's drainers return, **do not parse their replies**. Call
   **`brain_enrich_advance`** — the daemon drains every pushed result, applies it, and
   deletes those units from the queue.
4. Go back to step 1. Stop when `brain_enrich_pending` returns `{"pending": 0}`. If a
   full wave leaves `pending` **unchanged** (no progress — only live-leased or stuck
   units remain), stop and report `PARTIAL: units still pending — re-run to continue`;
   a stuck unit's 15-minute claim lease expires and the next run (or a re-run) sweeps
   it. **Backfill is just re-running this routine.** There is no wave cap.
5. Report: `DONE: queue empty` or `PARTIAL: units still pending — re-run to continue`.

Never pull unit payloads into your own context — each drainer claims and pushes its
own units (`brain_enrich_claim` → extract → `brain_enrich_push`). Use the MCP tools
only; do not read skill/command files or shell into the queue.
