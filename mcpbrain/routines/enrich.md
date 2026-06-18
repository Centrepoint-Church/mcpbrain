# Brain enrichment (hourly) — fan-out

Enrich the pending email batch through the mcpbrain MCP tools. You are the
**orchestrator**: you plan the work and hand each shard to a subagent, so your own
context never holds the email bodies — it only ever sees thread IDs and one-line
status replies. Self-contained — needs no skill or command file.

1. Call **`brain_enrich_manifest`**. If it returns `{"empty": true}`, stop and
   report `DONE: spool empty`.
2. Otherwise it returns `batch_id`, `thread_total`, and `shards` — a list of
   `{shard, thread_ids, with_blocks}`. Each shard is one unit of work. A shard with
   `with_blocks: true` also carries `block` (the one block type it handles) plus
   `block_start` and `block_count` (the slice of that block's items it owns).
3. For **each shard**, spawn a **subagent** (the Task tool, general-purpose).
   Spawn them in parallel — up to ~5 Task calls in one message, then the next
   batch — so the run finishes fast. Give each subagent EXACTLY this instruction,
   substituting the shard's values:

   > Automated enrichment of one shard — act autonomously, do not ask questions.
   > 1. Load the tools:
   >    `ToolSearch("select:mcp__mcpbrain__brain_enrich_pull,mcp__mcpbrain__brain_enrich_push")`.
   > 2. Call `brain_enrich_pull` with `batch_id=<batch_id>`,
   >    `thread_ids=<this shard's thread_ids>`, `with_blocks=<this shard's
   >    with_blocks>`, and (for a blocks shard) `block=<this shard's block>`,
   >    `block_start=<block_start>`, `block_count=<block_count>`.
   >    (Passing `batch_id` reads this run's frozen snapshot.)
   > 3. The result has a **`rules`** field — the FULL extraction protocol. Follow it
   >    EXACTLY: produce one extraction object per thread. If this is a blocks shard,
   >    answer the one block the pull returned (`merge_review` → `merge_answers`, or
   >    `synthesis` / `profile_synthesis` / `community_synthesis` / `memory_distil` /
   >    `profile_audit` → the field of the same name).
   > 4. Call `brain_enrich_push` with `batch_id=<batch_id>`, `shard=<this shard's
   >    index>`, `extractions=[…]`, and an answer field for each block present.
   >    Confirm `{"written": true}`.
   > 5. Reply with ONE line only: `shard <index>: <N> threads` (add `, blocks` if it
   >    handled the blocks shard). Nothing else.

4. When all subagents have replied, report:
   `DONE: batch <batch_id> — <thread_total> threads across <K> shards`.

Never pull thread bodies into your own context — each subagent pulls its own shard.
Use the MCP tools only; do not read skill/command files or shell into the spool.
