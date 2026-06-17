---
description: Hourly brain enrichment — pull the pending batch, extract per the returned rules, push results back.
---

# mcpbrain enrich

Hourly email enrichment, run entirely through the mcpbrain MCP tools. This command
is self-contained: it needs no skill file and no source repo.

1. Call **`brain_enrich_pull`**. If it returns `{"empty": true}`, stop and report
   `DONE: spool empty`.
2. Otherwise it returns `batch_id`, a `threads` list, a `context` block, and a
   **`rules`** field containing the FULL extraction protocol (envelope schema,
   entity/relation/merge rules). **Follow `rules` exactly** — produce one
   extraction object per thread.
3. Call **`brain_enrich_push`** with `batch_id` (verbatim from step 1),
   `extractions` (the list you built), and `merge_answers` (`[]` unless the batch
   asked for merge-review answers). Confirm the response is `{"written": true}`.
4. Report `DONE: batch <id> — N threads enriched` or `ERROR: <reason>`.

Use the MCP tools only — do not read skill files or shell into the spool. The
`rules` field from step 1 is authoritative; you never need anything else.
