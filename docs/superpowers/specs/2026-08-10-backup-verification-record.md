# Backup correctness + size — live verification record

**Date:** 2026-08-11. **Machine:** author box. **Store:** the real, live
`brain.sqlite3` (16.8 GB at the start of this run, actively serving Claude
Desktop's MCP connection throughout — the daemon was stopped only for the
reinstall + migration window below, never Claude Desktop itself).

This is the live gate Task 7 of
`docs/superpowers/plans/2026-08-10-backup-correctness-and-size.md` names as the
ship decision. All numbers below are measured, not projected.

## Sequence run

1. Stopped the daemon (`launchctl bootout gui/501/com.mcpbrain`).
2. `uv tool install --force ".[daemon]"` from this checkout at commit `bd93569`
   (Tasks 1–7's probe script; all six code tasks already committed and
   task-reviewed clean). Confirmed the installed build's `backup.snapshot`
   source is the `VACUUM INTO` version, not the old checkpoint-based one.
3. **Migration**, run directly against the stopped store rather than waiting
   on the real hourly cadence — draining ~8,183 files at the cadence's
   200/batch default would have taken roughly 41 hours, which is a pacing
   choice for steady-state operation, not a correctness requirement.
   `store.init()` performed the Task 3 rename-aside in 0.0s (confirmed
   metadata-only, as designed). `migrate_enrich_payloads_batch(limit=2000)`
   called in a loop, same code the daemon's cadence calls:

   | round | migrated | deleted | done | elapsed |
   |---|---|---|---|---|
   | 1 | 2000 | 9004 | False | 2.7s |
   | 2 | 2000 | 15116 | False | 33.6s |
   | 3 | 2000 | 13049 | False | 37.0s |
   | 4 | 2000 | 11887 | False | 41.0s |
   | 5 | 183 | 1043 | **True** | 41.2s |

   **Total: 8,183 files migrated, 50,099 legacy rows deleted, 41.2s.**
   `enrich_payloads_legacy` confirmed dropped.
4. Restarted the daemon on the new build. Confirmed clean startup (control API
   up, no new errors in `com.mcpbrain.err` beyond pre-existing/unrelated
   warnings) and that the process start time matched the restart, not a stale
   process.
5. Ran `bin/probe_backup_snapshot.py` (committed at `bd93569`, task-reviewed
   clean — see the SDD ledger) against the now-live, daemon-serving store.
6. Ran one full `make_encrypted_snapshot` → `restore` cycle with a throwaway
   Fernet key, entirely under `/tmp`, deleted afterward.

## Store size, before and after the migration

| | file | live (`_live_bytes`) | free disk |
|---|---|---|---|
| before migration | 16,044 MB | *(not measured; freelist was 4 MB pre-Task-3)* | 15,321 MB* |
| after migration | 16,044 MB | **2,524 MB** | 14,957 MB |

\* measured at session start before any code changes, on 2026-08-10, as
~13.19 GB; grew to ~15 GB by the time of this run through ordinary disk churn.

`PRAGMA freelist_count` after migration: **3,460,935** of 4,107,283 total
pages (84%) — confirming the file itself is unchanged in size (re-keying does
not shrink it) while `VACUUM INTO` now has almost the whole file to skip.

## Gate 1 — duration and footprint (`snapshot` arm)

```
{"file_mb": 16044, "live_mb": 2524, "wal_mb": 0}
{"arm": "snapshot_vacuum_into", "seconds": 22.3, "artifact_mb": 2478,
 "peak_rss_mb": 43, "wal_mb_before": 0, "wal_mb_after": 0,
 "stall_s_budget": 1800.0, "gate": "PASS"}
```

**22.3 seconds** against `STALL_S = 1800.0` — the cycle thread runs this, and
the gate was a hard stop at anything approaching half that budget (900s).
22.3s is **1.2% of the budget**. Peak RSS 43 MB. Artifact size (2,478 MB)
matches the live-byte estimate (2,524 MB) closely — the small delta is
ordinary overhead in `VACUUM INTO`'s rebuilt indexes.

**Verdict: PASS, by a wide margin.**

## Gate 2 — cause (R) closed (`pinned_reader` arm)

```
{"arm": "pinned_reader", "seconds": 29.1, "wal_mb_at_start": 0,
 "outcome": "SUCCESS — cause (R) is closed"}
```

This is the exact scenario that raised `RuntimeError:
wal_checkpoint(TRUNCATE) busy=1` before this work (Finding 3, measured
2026-08-05: busy=1 on 6 of 6 attempts, `checkpointed_frames: 0` every time). A
read-only connection held one `BEGIN` + `SELECT` on an older snapshot for the
whole 29.1s `snapshot()` call, against the real store, with the daemon
serving normally. **`snapshot()` succeeded.**

**Verdict: PASS — cause (R) is closed on the real store, not just in a unit
test.**

## Gate 3 — fidelity (`fidelity` arm)

```
{"arm": "fidelity", "source_before": 168096, "source_after": 168098,
 "artifact": 168097, "dim": 384, "fts_source": 73372, "fts_artifact": 73372,
 "knn_identical": true, "gate": "PASS"}
```

`source_before` (168,096) vs `source_after` (168,098) differ by 2 — the daemon
wrote during the window, exactly the reason the design compares the artifact
against the **pre**-snapshot fingerprint, not post. `artifact` (168,097) sits
inside that range, consistent with a point-in-time snapshot taken mid-write.
FTS match count identical (73,372 both sides). **KNN over the real
167,992-vector, dim-384 index returned bit-identical results** between source
and artifact for all sampled queries.

**Verdict: PASS.** The vec0 rebuild hazard named throughout this plan — a
renumbering `VACUUM` breaking the untyped-rowid shadow table — does not
manifest at real scale, corroborating Task 2's synthetic gap-forcing probe.

## Gate 4 — the free-space preflight

Before this work, on this exact box (`backup_state.json`, captured
2026-08-10): `[Errno 28] snapshot needs ~18763MB free ... but only 13188MB is
available; skipping this backup`.

After:

```
preflight PASSES
```

Live estimate 2,524 MB against 14,957 MB free — **the preflight now clears
with roughly 6x headroom**, where it previously refused outright.

## Gate 5 — one full encrypted cycle

`make_encrypted_snapshot` (store + records repo + config.json, tar+gzip,
Fernet-encrypted, throwaway key):

- **72.8s**, artifact **571.2 MB** (compressed+encrypted; ~2,478 MB store
  snapshot bundled with a 1.7 MB records repo and config.json).

`restore` (decrypt, unpack, place):

- **11.8s**, restored store **2,478.4 MB**, records and config both present
  at their target paths.

Opened the restored store and confirmed, independent of the source store:

```
chunks: 168099
enrich_payloads: 8183
legacy table exists: 0
journal_mode: wal
KNN query returned 5 rows: [...]
```

`journal_mode` came back `wal` — `Store.init()` converted the artifact's
`delete` mode on open, exactly as Task 1's design and tests describe.
`enrich_payloads` at 8,183 with no legacy table confirms the migration state
is carried correctly through a real encrypt → decrypt → restore round trip,
not just through the in-process test suite. The KNN query against the
restored store's `vec_chunks` returned real, sensible results.

**Verdict: PASS.**

## Summary

| Gate | Result |
|---|---|
| Migration drain | 8,183/8,183 files, 50,099/50,099 legacy rows, 41.2s |
| Snapshot duration vs `STALL_S` | 22.3s / 1800s (1.2%) — PASS |
| Cause (R) closed | SUCCESS under a held read transaction — PASS |
| Artifact fidelity (KNN + FTS) | identical — PASS |
| Free-space preflight | PASS (was failing before) |
| Full encrypted cycle + restore | PASS, verified independently |

**All five gates pass on the real store. The mechanism ships as designed —
`VACUUM INTO` with no fallback path needed.**

## What this does not change

Per the design's own scope: the backup bundle still omits `google_token.json`,
the `enrich_queue` spool, `models/`, `recall_seen/`, `connections.json`, and
`baseline_bootstrap.json` — a restore needs re-auth and replays pending
enrichment. Already-published fleet cache artifacts still carry
pre-deduplication payload sizes; only artifacts published after this migration
shrink. `com.mcpbrain.err` remains unrotated. None of these were in scope for
this plan and none were touched.
