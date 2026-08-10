# Backups: a consistent snapshot, and an artifact that fits on disk

**Status:** design approved 2026-08-10, not yet planned or implemented.
**Supersedes for the backup path:** the "distinguish frames-remained from truncate-failed, and/or
checkpoint under a longer busy_timeout" follow-up named in
`2026-08-04-mcp-server-process-lifecycle.md` § Finding 3 and item 1 of
`2026-08-10-tool-registry-thin-adapter-followups.md`. Both candidate shapes are **rejected here on
measurement** — see § Rejected alternatives.

## The problem, in two halves

`backup.snapshot()` runs `PRAGMA wal_checkpoint(TRUNCATE)` and raises on any nonzero `busy`, then
`shutil.copy2`s the main DB file. Both halves of that are defective, and separately the artifact has
grown past the free space on the machine.

**Half 1 — correctness.** Finding 3 measured two causes of `busy=1`. Cause (W), concurrent MCP
writers, was closed by 0.7.115's thin-adapter work. Cause **(R)** — a single held read transaction
blocks `TRUNCATE` absolutely — was not, and cannot be by anything already planned: `brain_graph` and
`brain_actions` read transactions (6.3 s / 3.1 s median on this store) outlive the 5000 ms
`busy_timeout` `_open_db` sets, and after 0.7.115 those reads execute *inside the daemon process*,
competing with the backup cadence in the same process. Re-probed 2026-08-10: `pinned_reader` still
returns busy=1 on 6 of 6 attempts with `checkpointed_frames: 0`.

Underneath that sits a second, unfixed hazard already named at `daemon.py:3876`: even when the
checkpoint returns cleanly, the multi-minute `shutil.copy2` that follows can be torn by a concurrent
`wal_autocheckpoint` writing pages into the main file. `_bulk_lock` holds off the cadence passes but
not the daemon's control-API threads — which, post-0.7.115, is exactly where routed tool writes now
run. **A `busy=0` result does not make the copy that follows it safe.** The current design's
guarantee is weaker than its docstring claims.

**Half 2 — size.** Measured on the live store 2026-08-10: the file is **15.65 GB** (the 11.9 GB in
`CLAUDE.md` is stale) against **13.19 GB** of free disk. The plaintext copy no longer fits at all,
which is why the preflight is refusing: `backup_state.json` reads
`[Errno 28] snapshot needs ~18763MB free … but only 13188MB is available`. This — not WAL
contention — is what is actually blocking backups on this box, and disk space is 57% of the 98
recorded `periodic backup failed` events (WAL contention is 3%).

## What was measured

All figures from the live store, 2026-08-10, read-only unless stated.

| measurement | result | what it settles |
|---|---|---|
| file size / free disk | 15.65 GB / 13.19 GB | the copy cannot fit; ENOSPC is structural |
| `freelist_count` | 1 025 pages = 4 MB (0.0%) | no whole free pages to reclaim |
| `dbstat` totals | 16 025 MB accounted, **279 MB unused within pages** | no fragmentation; a rebuild today buys 1.8% |
| `dbstat` by object | **`enrich_payloads` 13 587 MB = 85%**; everything else 2 438 MB | one table is the entire size problem |
| `enrich_payloads` rows | 50 099 rows / **8 183 distinct Drive files** = **6.1× duplication** | defect A |
| payload composition | avg 283 KB, max 3.6 MB; on the largest row `messages` is 3 609 551 of 3 610 680 B (99.97%), extracted output ~800 B | defect B (see § Deliberately not doing) |
| vec0 shadow rowids | `vec_chunks_vector_chunks00`: 274 rows spanning rowids 1–450, **176 gaps** | the VACUUM hazard is live-shaped, not hypothetical |
| VACUUM-INTO probe (gaps proven present first) | rowids **preserved**, `chunk_id` ↔ vector-rowid intact, KNN 6/6 identical, FTS and counts equal | the chosen mechanism is safe for vec0 |

A note on instruments, because this investigation produced two null ones. A sampled row-length
estimate put `enrich_payloads` at 835 MB; `dbstat` measured 13 587 MB — the head/middle/tail sample
missed the large rows by 16×. And the first VACUUM probe reported a clean pass while producing a
**single** vector-chunk row with no gaps, so a renumbering VACUUM would have been the identity map
and could not have failed. Both were corrected before anything was concluded. This is the same
failure mode as Finding 3's idle WAL measurement: *a green result from an instrument that could not
have gone red proves nothing.* Any probe added by the implementation must state the condition under
which it would fail, and assert that condition holds.

## Design

### Part 1 — `snapshot()` produces a consistent copy and never checkpoints

Replace the checkpoint-then-`copy2` body with SQLite's own consistent copy:

```python
db = _open_db(store_path, read_only=False)
db.execute(f"VACUUM INTO '{out_path}'")
```

- **Cause (R) and cause (W) both disappear**, because nothing needs the exclusive checkpoint any
  more. `VACUUM INTO` runs in an ordinary read transaction and reads *through* the WAL. The `busy`
  result, the `RuntimeError`, and the failure mode are deleted rather than made more patient.
- **The torn copy disappears**, because the artifact is built by the pager from one consistent
  snapshot rather than read off a file another connection may be checkpointing into.
- **Free pages are excluded**, which is what makes Part 2's reclaim automatic — see Part 3.
- **The source connection stays write-mode.** `mode=ro` is tempting (a backup that provably cannot
  touch the live store) but a read-only connection cannot create the `-shm` file when nothing else
  has the DB open — which is exactly how `bin/repair.py` and `bin/consolidate.py` run, with the
  daemon stopped. Rejected for that reason.
- **The destination must not exist.** `VACUUM INTO` errors outright if it does, so unlinking
  `out_path` and any stale `-wal`/`-shm` sidecar becomes a required step, not a defensive one. A
  mid-copy failure must also unlink the partial destination, preserving today's tested contract that
  no partial artifact ever looks successful.
- **The artifact is a freshly built DB**, so its header says rollback-journal where `copy2`
  preserved WAL. `init()` sets WAL when the restored store is opened, so this is benign — but it is
  a real behavioural difference and gets its own test.
- **A targeted smoke check on the output**, which a page copy would not have justified but a
  *rebuild* does: row-count parity on `chunks` plus one `vec0` KNN query against the artifact. Cheap
  relative to the rebuild, and aimed precisely at the risk this mechanism introduces. Not a full
  `integrity_check` — that would re-read the whole artifact for a hazard it is not the best detector
  of.

`_bulk_lock` stays, with a **rewritten rationale**. Its current justification (`daemon.py:3868-3879`)
is entirely the busy-abort and the torn copy, both now gone. It still earns its place: the snapshot
pins a read transaction for minutes, and holding the chunk-writing passes off during that window
bounds WAL growth and I/O contention. The daemon comment and `test_daemon.py:1233`'s docstring both
assert the old reason and must be corrected, not left — the behaviour stays right while its stated
justification becomes false.

`bin/repair.py:70` and `bin/consolidate.py:37` route through `snapshot()` and inherit the
consistency guarantee for free; their checkpoint-explaining comments need updating.

### Part 2 — `enrich_payloads` is keyed per file, not per chunk

`drain.py:533-536` serialises the whole-unit extraction and writes it **once per chunk `doc_id`**:

```python
_drive_docs = [d for d in doc_ids if d.startswith("gdrive-")]
if _drive_docs:
    _payload = json.dumps(extraction, sort_keys=True)
    for _d in _drive_docs:
        store.set_enrich_payload(_d, _payload, ENRICH_LOGIC_VERSION)
```

Since 0.7.98 a Drive file's `doc_ids` resolve to *every chunk of that file*, so a 2 303-chunk PDF
stores 2 303 identical copies. The consumer reads exactly one — `ingest_cache.publish_file`'s own
comment: *"one payload per file — chunks share the unit's extraction, so the lookup stops at the
first hit."* Written N times, read once, at 6.1× on this store.

The fix is to make the storage match the access pattern:

- `enrich_payloads` is keyed on **`file_id`**, one row per Drive file.
- `store.set_enrich_payload` / `get_enrich_payload` take a `file_id`.
- `drain.py` writes once per file instead of looping `_drive_docs`.
- `ingest_cache.publish_file` drops its per-chunk lookup loop for a single call.
- The two delete-by-`doc_id` paths — `store.py:1443` and `ingest_cache.py:210` — resolve to the file
  key. These are the paths that keep the cache correct when a doc is deleted or a drive revoked;
  getting them wrong leaves stale payloads that outlive their documents.

This is a bug fix, not a retention policy. No TTL, no expiry judgement, no data whose loss needs
weighing: the removed rows are byte-identical duplicates of a row that is kept.

### Part 3 — migration, and why it must be idempotent

The migration is cheap and needs no attended step and no stop-and-swap: delete the 41 916 redundant
rows keeping the highest `logic_version` per file (tie-break newest `at`), canonicalise the 8 183
survivors' keys, and `ALTER TABLE … RENAME COLUMN` — metadata-only. No 2.2 GB table copy.

**The key is the bare `file_id`**, not `gdrive-<file_id>`: that is what `publish_file` already holds,
what `chunks.metadata.$.file_id` stores, and what `idx_chunks_fileid` indexes. Deriving it from an
existing row means stripping the trailing chunk index from `gdrive-<file_id>-<idx>` — and a Drive
`file_id` can itself contain `-`, so this must strip **only a trailing `-` followed by digits**
(`^gdrive-(.+)-\d+$`, non-greedy on the suffix), never split on the first or every hyphen. A row
whose key does not match that shape is left untouched under its existing key: the table has only
ever been written by `drain.py`'s `gdrive-` filter, so an unmatched row means an assumption has
broken, and the migration must not invent a key for it.

It frees ~11.3 GB **onto the freelist**, and the file stays 15.65 GB until something reclaims it.
Nothing needs to: Part 1's `VACUUM INTO` excludes free pages, so every artifact is built from live
data only. That is what removes the fleet rollout problem — no install needs an attended VACUUM, a
stop-and-swap, or any migration beyond the wheel. It also means the two halves are not independent:
**Part 2 without Part 1 leaves backups copying 15.65 GB, and Part 1 without Part 2 leaves them
copying 15.65 GB of which 71% is duplicate cache.** Neither ships alone.

**The migration must run from `init()`, idempotently, against whatever store it finds — not once per
install behind a version marker.** An artifact captured before this change carries the old
`enrich_payloads(doc_id …)` schema, and restoring it into a post-fix build has to work. A one-shot
keyed to a marker would skip a restored old-shape store and leave the new code reading a column that
no longer means what it says.

**Expected end state:** 50 099 rows → 8 183; `enrich_payloads` 13 587 MB → ~2 220 MB, so the artifact
is built from ~4.7 GB of live data instead of a 15.65 GB copy; the free-space preflight drops from
~18.8 GB to roughly 6.5 GB against 13.19 GB available, clearing ENOSPC with headroom.

## Rejected alternatives

**Classify the busy result (`checkpointed_frames == log_frames` means the data is safe).** Named as
a candidate by Finding 3 and by the followups doc. Rejected: it fixes only the **(W)** signature,
and (W) is already gone as of 0.7.115. Cause (R) measured `checkpointed_frames: 0` on 6 of 6
attempts, so classification would still abort it. Shipping this alone would be a fix for the cause
that no longer occurs.

**Checkpoint under a longer `busy_timeout` while holding the bulk lock.** The other named candidate.
It would help (R) — the backup already holds `_bulk_lock` for the whole snapshot and upload, so the
marginal hold of a 30 s retry is small against a multi-minute baseline, which answers the
"does retrying extend bulk-lock contention" objection. Rejected anyway because it treats the
symptom: it makes an exclusive checkpoint more patient rather than removing the need for one, and it
does nothing about the torn copy, which is the more dangerous of the two defects because it fails
*silently*.

**`sqlite3.Connection.backup(pages=-1)`.** A genuine contender and the original recommendation here:
a page copy at near-raw speed with no schema semantics and so no rebuild risk. Fixes (R), (W) and
the torn copy identically. Rejected because it copies freelist pages, so post-dedup it would still
move 15.65 GB, and reclaiming would need an attended `VACUUM`-and-swap that the rest of the fleet
cannot run. (If `VACUUM INTO` fails its live gate, this plus a one-shot reclaim script is the
fallback — a decision point in the plan, not a shipped hedge.) Note if it is ever revisited:
`pages=-1` would be mandatory, because a multi-step backup is restarted by SQLite whenever another
connection writes the source and would never converge on a store written every few seconds.

**`VACUUM INTO` purely as compaction, before the dedup.** Measured: 279 MB of 15.65 GB, 1.8%. Worth
nothing on its own. Its value here is entirely a consequence of Part 2 creating a large freelist —
which is why the two parts ship together.

**Stripping message bodies from the payload.** Would take the table to ~20 MB rather than ~2.2 GB,
since `graph_write.apply` consumes only message *headers* (`sender`, `to`, `cc`, `date`,
`message_id`) and the bodies are read solely by `_grounding_filter`, gated on `schema_grounding`
which defaults OFF and already fails open on empty source. Deliberately **not** taken: it weakens a
defence-in-depth control on peer-supplied payloads (`ingest_cache.py:225-241` validates a peer's
extraction through the same guards drain uses, *precisely* so a peer's payload is never applied raw)
for a saving that is not needed once duplication is fixed. Recorded here so the option and its
measurements are not lost.

## Testing

TDD. Every test below is written and failing before the corresponding change.

**Part 1 — the copy**

1. **Cause (R), the whole point, and fully deterministic.** A second connection holds `BEGIN` +
   `SELECT` on an older snapshot while the store commits more rows; call `snapshot()`. Today:
   `RuntimeError: wal_checkpoint(TRUNCATE) busy=1`. After: an artifact containing every pre-snapshot
   commit, `integrity_check` ok. This is the `pinned_reader` probe arm reduced to a unit test.
2. **Consistency under a concurrent writer.** A writer thread commits throughout the copy; assert
   `integrity_check` ok and every pre-call commit present. Honest caveat: making this a true RED
   against the old code needs a deliberately slowed `copy2` plus enough pages to trip autocheckpoint
   mid-copy. Attempt that as the RED; if it proves flaky, keep it as a positive guard and **say so
   in the plan** rather than claim a reproduction that was not obtained.
3. **vec0 and FTS survive the rebuild** — a store with vec0 vectors and FTS rows, with **gaps forced
   in `vec_chunks_vector_chunks00`** and the gaps asserted present before the snapshot (else the
   test is a null instrument), then KNN and FTS results compared across the artifact.
4. **Invert `test_checkpoint_runs_before_copy`** to assert **no** `wal_checkpoint` is issued. That
   test currently pins the defect in place; relaxing it is not enough, it has to invert or the
   exclusive checkpoint gets reintroduced later.
5. **Replace `test_snapshot_raises_on_busy_checkpoint_no_partial_file`** with a failure-mid-copy
   version: force the copy to raise, assert `out_path` absent.
6. **Destination hygiene** — a pre-existing `out_path` and stale `-wal`/`-shm` are cleared.
7. **Artifact journal mode** — the restored store opens and ends in WAL after `init()`.
8. Unchanged and still green: the four existing "snapshot contains the latest committed writes"
   tests, and `test_consolidate.py:167`.

**Part 2 — the payload**

9. One Drive file with N chunks produces **one** row, not N.
10. `publish_file` still attaches the payload, via the single-call lookup.
11. Both delete paths (`store.py:1443`, `ingest_cache.py:210`) remove the file's payload.
12. **Migration idempotence and restore compatibility** — build an old-shape store (per-chunk
    `doc_id` rows, duplicates present), open it, confirm it collapses to one row per file, that
    running `init()` again is a no-op, and that `publish_file` works afterwards. This is the test
    that protects restoring a pre-fix artifact.

## Live verification gate

This decides whether the mechanism ships, and runs before release, not after.

- **Duration and footprint of `VACUUM INTO` on the real store**, against `shutil.copy2` as the
  baseline: wall time, peak RSS, WAL growth across the window. Compare the snapshot phase against
  `STALL_S = 1800.0` — the cycle thread runs this, and a rebuild slow enough to approach the
  watchdog window means redesign, not ship.
- **`snapshot()` succeeding while a pinned read transaction is held** — the direct proof that cause
  (R) is closed for backups rather than merely closed in a unit test.
- **Artifact fidelity on real data** — chunk-count parity and a real KNN query against the artifact
  built from the live store, plus an FTS query. The synthetic probe used 6 000 vectors of dim 8; the
  live store has 167 992 of dim 384 across 274 vector chunks.
- **One full `make_encrypted_snapshot` cycle** in the 0.7.113 verification shape: artifact size,
  peak temp, decrypt-and-open.
- **Post-migration counts** — `enrich_payloads` at 8 183 rows, and the artifact built from ~4.7 GB.

## Risks

- **The rebuild is the mechanism's own risk.** Mitigated by test 3, the live fidelity check, and the
  runtime smoke check — three layers, because a silently wrong vector index would be discovered only
  at restore.
- **Duration.** A logical rebuild is slower than a page copy; bounded by the gate above.
- **WAL growth while the rebuild pins a read transaction.** `_bulk_lock` holds off the heavy
  writers, leaving the small control-API writes; measured in the gate. A new standing cost, not
  zero.
- **The migration touches a 13.5 GB table on a live store.** It runs from `init()`, must be
  incremental, and must never run inside the backup path.
- **No kill switch.** A single path was chosen deliberately over a flag; the live gate is the safety
  net, on a package that auto-updates unattended.

## Observations recorded, not fixed here

- **A restore does not reconstitute the whole app dir.** The bundle is `store/brain.sqlite3` +
  `records/` + `config.json`. Not included: `google_token.json` (a restore needs re-auth), the
  `enrich_queue` spool of pending extractions (replayed, since those chunks stay `enriched=0`),
  `models/`, `recall_seen/`, `connections.json`, `baseline_bootstrap.json`, and operational
  state/logs. Unchanged by this design; worth a deliberate decision of its own.
- **The fleet's shared-drive cache artifacts carry the same bloated payloads**, since
  `publish_file` attaches whatever `get_enrich_payload` returns. Part 2 shrinks what is published
  going forward; already-published artifacts are not rewritten.
- `com.mcpbrain.err` is 334 MB and still unrotated (also noted in the Finding 3 spec).

## Success criteria

1. A backup completes with a read transaction held open across it — the failure mode Finding 3 left
   unfixed.
2. No `wal_checkpoint` runs in the backup path at all.
3. The artifact restores to a store whose KNN and FTS results match the source.
4. `enrich_payloads` holds one row per Drive file, and a pre-fix artifact restores and migrates.
5. The free-space preflight passes on this machine, with the measured numbers recorded.
