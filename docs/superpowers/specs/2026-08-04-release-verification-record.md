# Verification record — mcp 2.x migration + backup hardening (unreleased)

Covers the 31 commits on `main` since release **0.7.112**. Version deliberately **not**
bumped; **nothing pushed**. This is the evidence a release decision should rest on, plus the
residual risks it should weigh.

Two workstreams:
- **(A)** `mcp` 1.x → 2.x migration of the MCP server, executed against
  `docs/superpowers/plans/2026-08-04-mcp-2-migration.md` with a per-task review gate.
- **(B)** backup / auth / daemon hardening, authored concurrently with no plan and **no review
  gate** until the final whole-branch review, which found five Important defects in it.

---

## (A) MCP surface — verified live against real Claude Desktop

Captured from the **installed wheel**, not the working tree:

```
capabilities: {"prompts":{"listChanged":false},
               "resources":{"listChanged":true,"subscribe":false},
               "tools":{"listChanged":false}}
serverInfo:   {"name":"mcpbrain","version":"0.7.112"}
protocol:     2025-11-25
prompts:      enrich, meeting-packs, gardener, reference-gardener, draft-reply
tools:        26 | annotated 26/26 | outputSchema 13
openWorld:    ['brain_meetings_today']
destructive:  ['brain_gardener_apply','brain_enrich_advance']
stderr:       0 bytes
```

Real Desktop handshake: `initialize`→result, `notifications/initialized`, `tools/list`→result,
**`prompts/list`→result** (never sent before this work), `resources/list`→result, 0 tracebacks.
`mcpbrain doctor` fully green — Enrichment moved from "Idle — nothing in 3 days" to "Running"
once the connection was restored.

`protocol: 2025-11-25` is the **success** condition, not a shortfall: `mcp` 2.x is dual-era, so
a ported server stays wire-compatible with the client that broke on 2026-08-04.

## (B) Backup path — verified live end to end

Nothing in the branch had put a byte through a real resumable Drive upload; every test used a
fake `files()`. Two live runs closed that.

**1. Isolated resumable upload** (throwaway Drive folder, so no artifact could enter the backup
namespace where `find_latest_snapshot` would treat it as newest):
33.6 MB v3 archive = 4× 8 MiB chunks → 6.0 s @ 5.6 MB/s, Drive size byte-exact, downloaded
back and decrypted with head match. File + folder + temp all removed.

**2. Full cycle on the real 11.92 GB store:**

| | |
|---|---|
| Total | 11.8 min (6.6 snapshot + 5.2 upload) |
| Artifact | 4.25 GB, `MCPBRAIN-ENC-v3` |
| Upload | 13.5 MB/s, Drive size `4246121008` == local **exactly** |
| Peak RSS | **226 MB** |
| Peak temp | **11.92 GB — exactly one store copy** |
| Min free | 27.24 GB of 40.30 |

Peak RSS matters specifically: the pre-existing multipart path flattened a 4.24 GB body into one
`BytesIO`, which `_default_media`'s docstring records as **97 upload failures against 52
successes** between 2026-06-25 and 2026-08-04, in storms of up to 57/day. 226 MB confirms the
resumable path does not buffer the artifact. Peak temp confirms the streaming-tar claim that the
plaintext bundle is never materialised. `_require_free_space` (the new cross-platform guard) was
exercised in production conditions and passed.

### The archive-format decision, validated on real data

The per-user Drive folder now holds 8 snapshots: the **newest is v3**, the **other 7 are v2**.

- The fix wave's first attempt changed the frame header 5→21 bytes **while keeping the
  `MCPBRAIN-ENC-v2` magic** — which would have made all 7 **silently misparse** (demonstrated:
  16 bytes dropped, no error, when payload bytes align with the v3 offsets).
- Shipping v3 **without** a legacy reader would have left 7 of 8 backups unreadable.
- What shipped — v3 for writes, a labelled read-only v2 path, and a hard
  `UnsupportedArchive` on any other magic — keeps all 8 restorable. Verified by running the
  **shipped** `decrypt_file` over genuine frames read out of the live 4.2 GB archive, read-only.

---

## Residual risks for the release decision

1. **`mcp` 2.0.0 is 8 days old** (released 2026-07-28) and brings **5 new transitive
   dependencies** — `httpx2`, `httpcore2`, `mcp-types`, `opentelemetry-api`, `truststore` —
   into a package that auto-updates **unattended, daily**. `mcp>=2.0,<3` bounds the direct dep
   and `mcp` hard-pins `mcp-types`, but the rest are unbounded transitives. Worth one look at
   resolved `uv.lock` versions before the wheel goes out, since a bad transitive resolve is
   exactly the failure this branch exists to prevent.
2. **The Windows hardware QA gate stays open.** `os.statvfs` → `shutil.disk_usage` removes one
   reason it would have reopened (on Windows, *every* backup previously raised, was swallowed,
   and silently never ran) — but it is verified by a simulated-Windows unit test, not hardware.
3. **The v2 legacy read path must outlive the last v2 archive**, and nothing forces its removal.
   Retention ageing out is the **wrong** trigger: `download_and_restore` and both CLI restore
   paths can be pointed at any archive a user still holds. Removal must be deliberate.
4. **Existing v2 archives remain spliceable** until they age out. Not papered over — refusing
   them would have stranded the newest good backup.
5. **`num_retries=0` removes the only in-flight upload retry**, so a mid-upload blip now costs a
   full ~4.25 GB re-upload one interval later. Correct trade against a 600 s wedge with watchdog
   recovery deferred, but a self-driven re-seeking `next_chunk` is the proper follow-up.
6. **A shipped fix does not reach users when the wheel lands** — see
   `2026-08-04-mcp-server-process-lifecycle.md`. A live MCP server runs its start-time code
   indefinitely, so the effective delivery event is the user's next client restart. This is the
   most under-appreciated risk here, because it applies to *every* release.
7. **Backups were failing before this work** (`wal_checkpoint(TRUNCATE) busy=1`, newest good
   archive 12:27) for reasons unrelated to this branch. The full cycle above succeeded, so the
   condition is intermittent. `CLAUDE.md` also records daemon cadence passes appearing stalled
   since 2026-07-23, which would suppress the backup cadence independently.

## Known-good invariants a future change must not break

- `tool_schemas()` is the single source for both the advertised tool list and validation —
  `mcp` 2.x's low-level server validates **nothing** on its own.
- The validator must never inject defaults or mutate the arguments dict (`brain_enrich_push`
  distinguishes an absent `extractions` from an empty one; four guards depend on it).
- `on_call_tool` has exactly 3 returns and 1 `except ValueError`, scoped to the validate call
  only — a blanket catch around the dispatch would dress a real handler bug as a tidy error.
- Validation failures return `isError`, not a raised exception (a bare `ValueError` gets
  `code=0` plus a ~20-line traceback into the fleet's MCP log).
- `mcp_server.py` must stay free of native/heavy imports; `embedder_dim` stays function-local.
- Four parallel per-tool mappings (`tool_schemas`, `_TOOL_DESCRIPTIONS`, `tool_annotations`,
  `tool_output_schemas`) are fenced by set-equality guard tests. Consolidating them into a
  `TOOL_SPECS` record is the agreed end state but was **deliberately deferred**: it would
  invalidate the live verification above and wants its own protocol round-trip as a gate.
