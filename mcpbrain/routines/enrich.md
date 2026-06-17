# Brain enrichment (hourly)

Enrich the pending email batch through the mcpbrain MCP tools. Self-contained —
needs no skill or command file.

1. Call **`brain_enrich_pull`**. If it returns `{"empty": true}`, stop and report
   `DONE: spool empty`.
2. Otherwise it returns `batch_id`, a `threads` list (a size-bounded slice — see
   `threads_returned`/`threads_total`/`more`), a `context` block, and a **`rules`**
   field with the FULL extraction protocol (envelope schema, entity/relation/merge
   rules). **Follow `rules` exactly** — produce one extraction object per thread in
   the returned slice.
3. Call **`brain_enrich_push`** with `batch_id` (verbatim), `extractions` (your
   list), and `merge_answers` (`[]` unless the batch asked for merge-review).
   Confirm `{"written": true}`.
4. Report `DONE: batch <id> — N threads enriched`. If `more` was true, there are
   further threads; the daemon re-prepares the next slice for the next run.

Use the MCP tools only — do not read skill/command files or shell into the spool.
