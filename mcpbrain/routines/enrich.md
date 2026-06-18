# Brain enrichment (hourly)

Enrich the pending email batch through the mcpbrain MCP tools. Self-contained —
needs no skill or command file.

1. Call **`brain_enrich_pull`**. If it returns `{"empty": true}`, stop and report
   `DONE: spool empty`.
2. Otherwise it returns `batch_id`, a `threads` list (a size-bounded slice — see
   `threads_returned`/`threads_total`/`more`), a `context` block, a **`rules`**
   field with the FULL extraction protocol, and possibly extra request blocks:
   `merge_review`, `synthesis`, `profile_synthesis`, `community_synthesis`,
   `memory_distil`, `profile_audit`. **Follow `rules` exactly**: produce one
   extraction object per thread, AND — for every request block the pull included —
   produce that block's answers as the rules describe. Skip a block only if it
   wasn't in the pull.
3. Call **`brain_enrich_push`** with `batch_id` (verbatim), `extractions`, and an
   answer field for **each block that was present**: `merge_answers`,
   `synthesis`, `profile_synthesis`, `community_synthesis`, `memory_distil`,
   `profile_audit`. Omit a field if its block wasn't in the pull. Confirm
   `{"written": true}`.
4. Report `DONE: batch <id> — N threads enriched`. If `more` was true, there are
   further threads; the daemon re-prepares the next slice for the next run.

Use the MCP tools only — do not read skill/command files or shell into the spool.
