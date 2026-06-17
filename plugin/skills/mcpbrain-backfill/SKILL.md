---
name: mcpbrain-backfill
description: Backfill enrichment of your full email history. Processes the spool in batches, each in a fresh-context subagent so large histories don't hit context limits. Loops until the spool is dry.
---

# Backfill enrichment

Processes all pending email threads in the mcpbrain spool, one batch at a time, each in a fresh-context `enrich-batch` subagent so large histories never hit context limits.

## How it works
1. Check the spool: read `$(mcpbrain home)/enrich_queue/pending.json` (always resolve the home with `mcpbrain home` — it is `~/Library/Application Support/mcpbrain` on macOS, `%APPDATA%\mcpbrain` on Windows, NOT `~/.mcpbrain`).
2. If non-empty: dispatch the `enrich-batch` subagent; wait for its status line.
3. Wait ~60s for the daemon to drain the result and prepare the next batch (it writes `enrich_inbox/<batch_id>.json`, applies it, stamps `logs/enrich.log`, prepares the next `pending.json`).
4. Repeat until `pending.json` is absent/empty or the subagent returns `DONE: spool empty`.
5. After 3 consecutive empty checks, stop and report total progress.

## Loop
```
WHILE spool not dry AND empty_checks < 3:
  result = run_subagent("enrich-batch")
  IF "spool empty" in result OR pending.json absent: empty_checks += 1
  ELSE: empty_checks = 0; record status
  WAIT ~60s for the daemon to drain + prepare the next batch
REPORT: batches processed, final spool state, last drain log line
```

## Checks
```bash
H="$(mcpbrain home)"
[ -s "$H/enrich_queue/pending.json" ] && echo PENDING || echo EMPTY
tail -5 "$H/logs/enrich.log" 2>/dev/null || echo "(no drain log yet)"
```

Stop early on `/stop` or three consecutive `ERROR:` lines.
