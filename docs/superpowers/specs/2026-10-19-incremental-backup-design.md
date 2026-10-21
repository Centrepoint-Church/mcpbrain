# Incremental backup: local page-delta capture, resumable upload, one location

Date: 2026-10-19. Status: approved design (owner, in session), implementation follows.

## Why

1. **Every backup is a full 750 MB upload.** `make_encrypted_snapshot` does
   `VACUUM INTO`, tars, gzips and encrypts one stream, so any change rewrites
   the whole artifact. On a laptop on a hotspot that failed four days running
   (2026-10-15..18: Errno 49, broken pipe, read timeout) and left the store
   five days unprotected. 0.7.143+1 made the single upload resumable
   in-process; that does not survive a lid close, a reboot or a daemon restart.
2. **Uploads and auto-restore use different folders, and fresh installs cannot
   upload at all.** Verified live 2026-10-19:
   - The daemon uploads to `<backup.shared_drive_id>/<backup.user_id>/`. On the
     owner's machine that is `MCPBrain Backups/<backup.user_id>/` (the owner's per-user folder) (7 current
     snapshots).
   - Auto-restore (`restore.detect_restorable` → `find_latest_in_subfolder`)
     reads `<escrow folder>/<account email>/`, which holds ONE snapshot from
     **2026-06-16**. A new laptop would silently restore a four-month-old brain.
   - `backup_setup.enable_backup` (the wizard path) writes the escrow FOLDER id
     into `backup.shared_drive_id`, and `upload_snapshot` passes it as
     `driveId=` with `corpora="drive"`, which Drive rejects. The shared drive
     holds escrow keys for three other people and **no backup folder for any
     of them**.
3. **The upload holds the bulk lock.** `maybe_backup` runs snapshot + upload +
   prune under `_bulk_lock` with the watchdog muzzled, so a slow upload stalls
   the chunk-writing passes for its whole duration.

## Measurements (live store, 2026-10-19; scratch scripts, not committed)

| Comparison | Unchanged | Upload needed (compressed) |
|---|---|---|
| Two VACUUM INTO snapshots, 1 day apart | 14% of pages in place | 483 MB best case (CDC 64 KiB) vs 551 MB full |
| Two VACUUM INTO snapshots, 25 min apart | 25% | 406 MB vs 545 MB |
| Two `Connection.backup()` copies, 1 h apart | **97.9%** | **18 MB** at 8 KiB-page granularity |

- `VACUUM INTO` renumbers pages every time; interior and overflow pages carry
  page numbers, so 70–75% of the vector table "changes" even in 25 minutes. No
  chunker recovers that. `backup()` copies page-for-page and keeps the layout.
- Writes are scattered single pages: for the same hour, 32 KiB blocks cost
  37 MB, 128 KiB 82 MB, 1 MiB 265 MB. **The unit is one page.**
- `backup()` of the 2 GB store: 5 s. `VACUUM INTO`: 84 s.
- Store composition (compressed): vectors 349 MB, chunk text 81 MB, indexes
  32 MB, FTS 70 MB, irreplaceable graph/enrichment/actions/context ~62 MB.
  Leaving vectors out was rejected: re-embedding ran at ~4 chunks/s with the
  daemon busy, i.e. hours (up to ~17 h) before semantic search worked again
  after a restore.
- zlib level 6 on scattered pages: 81 MB vs zstd-3 77 MB (2.8 s vs 0.3 s per
  246 MB). Stdlib zlib is used; no new dependency.
- Still to measure: the 24-hour delta (a page-stable copy from 17:35 is kept
  in the session scratchpad). It sets the default base threshold only; the
  design does not depend on it.

Prior art surveyed (restic, Borg, Kopia, rustic, Duplicacy, Litestream v0.5,
sqlite3_rsync, the session extension): page-level deltas on a page-stable copy
is Litestream's LTX model and sqlite3_rsync's premise; Duplicacy recommends
fixed-size chunks for databases. Content-addressed dedup with garbage
collection (restic/Kopia) was rejected: it buys nothing here over page deltas
and its prune is the riskiest part of those systems. No tool has a Drive
backend that fits (Litestream: none; restic/Kopia need rclone and a separate
OAuth, and have no Windows-on-ARM builds).

## Design

Two independent jobs joined by a local **outbox**. Every step is short,
idempotent, and journaled, so a laptop that sleeps, reboots or loses its link
at any point resumes where it stopped.

### 1. Location: one resolver for every path

`backup_location.resolve(home, drive_service) -> Location(drive_id, root_id,
person, folder_id, legacy_folders)`:

- `drive_id`: the shared drive that holds backups. `backup.shared_drive_id` if
  it is a drive id; if it is a folder id (the wizard bug), the folder's
  `driveId`.
- `root_id`: the drive root (`drive_id`).
- `person`: the account email (`restore.account_email`), lower-cased. Same key
  the escrow key already uses.
- `folder_id`: `<drive root>/<person>/`, created on first upload.
- `legacy_folders`: every place a single-file backup may sit:
  `<drive root>/<backup.user_id>/` and `<escrow folder>/<person>/`.

The escrow key is written as `<escrow folder>/<person>.key`, the name restore
already reads (`enable_backup` wrote `<user_id>.key`, so a wizard install whose
user_id was not its email could never auto-restore its key).

Upload, prune, startup restore, `mcpbrain restore`, `restore --auto` and
`/api/backup/auto` all go through it. Restore considers the new layout AND every
legacy folder and picks the **newest restore point by capture time** across all
of them; it never prefers a location over a newer backup.

### 2. Capture (local, every `capture_interval_s`, default 3600)

Runs on the cycle thread. Holds `_bulk_lock` only for the `backup()` copy.

1. Free-space check (`page_count × page_size × 1.1` in the work dir), then
   `Connection.backup()` of the live store into
   `app_dir()/backup/work/capture.sqlite3`. Consistent: one read transaction.
2. Read the copy page by page. For each page: blake2b-128 hash, compare with
   `backup/pages.idx` (16 bytes per page; absent = everything changed). Also
   compute SHA-256 of the whole copy (the restore target).
3. Records + config: hash a deterministic listing (relative path, size, sha256)
   of the records repo (including `.git`) and `config.json`. If the hash
   differs from the last captured one, write a `records` item: an encrypted
   tar of both.
4. Write changed pages as one or more **page segments** of at most 16 MiB of
   compressed payload into `backup/outbox/`, each written to a temp name and
   renamed. Then write the item's sidecar `<item>.json` (kind, chain, seq,
   capture time, page_count, page_size, db_sha256, segment list, records item).
   The sidecar's rename is the item's commit.
5. Only then replace `pages.idx` (temp + rename) and delete the copy.

A crash before step 5 leaves `pages.idx` describing the previous capture: the
next run redoes the capture and any orphan temp/segment files without a sidecar
are swept. Cost of redoing: seconds.

**Base or delta.** A capture is a **base** (every page) when: there is no
chain; `page_size` changed; or the chain's committed delta bytes exceed
`base_threshold` (default 2.0, R37) × the chain's base bytes. Otherwise it is a
**delta** against the previous capture. A base is just a large item; it is
written locally in seconds and uploaded in many short sessions like anything
else.

**Chain continuity.** Every item records the seq it follows. If the outbox or
journal shows a gap (an item lost locally, `pages.idx` missing, a store swapped
in by restore or rebuilt by `bin/optimise_store.py`), the next capture is a
base. A delta is never written against a state the uploaded chain does not
contain.

**Coalescing.** Before uploading, any run of two or more consecutive delta
items of the same chain that have not started uploading is merged into one
(newest page wins, newest page_count / db_sha256 / capture time). A laptop
offline for three days uploads the pages changed in those three days once.
Merge writes the new item then removes the old ones; the sidecar rename is the
commit.

Segment payload (before encryption): magic `MCPBRAIN-PAGES-1\n`, a JSON header
line (chain, seq, kind, page_size, page_count, first/last pgno), then records
of `>I` pgno + raw page, zlib-compressed. Encrypted with the existing v3 frame
format (`backup.open_encrypted`). The ciphertext SHA-256 and MD5 are recorded
in the sidecar.

### 3. Upload (cycle thread, outside the bulk lock, time-boxed)

Each loop iteration spends at most `upload_budget_s` (default 60 s) on:

1. Pick the oldest outbox item. Upload its files in order, each by Drive's
   resumable protocol, implemented directly over the authorised HTTP client
   (`POST …/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true`
   → session URI; `PUT` with `Content-Range`; 308 + `Range`; status query
   `bytes */size`).
2. The session URI and confirmed offset are written to `backup/journal.json`
   after every confirmed chunk. On wake, restart or a new iteration, an
   in-flight file resumes by status query. A session Drive no longer knows
   (404/410) restarts that file from zero.
3. Chunk size adapts: 32 MiB, halving to 8 MiB after a failed chunk, growing
   back after 3 clean ones (multiples of 256 KiB).
4. A file is done when Drive's response `size` and `md5Checksum` equal the
   local ones. Its file id goes into the journal and the local file is
   deleted.
5. When every file of an item is done, upload the item's **manifest** (small,
   encrypted, simple upload) and verify it the same way. The manifest upload is
   the commit point: `backup_state.json` `ok=True`, `last_success` = commit
   time, `restore_point` = capture time.

Transient errors (the sync stack classifier plus HTTP 500) back off
5 s → 4 min and stop the iteration; permanent errors (403 on a missing
permission, 404 folder) are recorded in `backup_state.json` and surfaced.

**Manifest** (one per committed item, `manifest-<chain>-<seq:06d>`, encrypted
JSON): format 1, chain, seq, capture time, page_size, page_count, db_sha256,
and the complete ordered list of files needed to rebuild this restore point
(base segments, then every delta up to seq, then the newest records item), each
with Drive file id, size, ciphertext sha256. A manifest is self-contained:
restore needs that one manifest and the files it names. No global index.

Drive layout:

```
MCPBrain Backups/<person>/
  chain-<YYYYMMDD>-<rand>/      one folder per chain
    base-0001 … base-00NN       page segments
    delta-000002-0001 …         page segments
    records-000005              records + config tar
    manifest-<chain>-000001 …   commit records
```

### 4. Prune (stepwise, after a commit)

- Keep every chain that holds a restore point newer than `retain_days`
  (default 7; the old `retain` count is read as days when `retain_days` is
  absent), plus the newest chain always.
- A chain older than that whose successor chain is committed is deleted by
  deleting its folder (one call). A **pending deletion list** in the journal
  makes an interrupted prune continue.
- Hold rule carried over: do not delete a chain if the newest chain's base is
  under 0.5× the previous base (`prune_held`).
- Legacy single-file backups in every legacy folder are deleted once the new
  layout has a committed restore point older than `retain_days`. Until then
  they stay restorable.
- Never infer "absent from a listing" as a reason to delete or re-upload:
  Drive listings lag. Everything is addressed by recorded file id.

### 5. Restore (resumable, verified)

1. Resolve candidates: newest manifest per chain in the new layout, plus every
   legacy `.enc` file. Choose the newest by capture time (legacy: createdTime).
2. New layout: download each named file to `<app_dir>/backup/restore/` with
   Range-resume, journaling progress; verify ciphertext sha256; decrypt;
   write pages into a staging file at `(pgno-1) × page_size`; apply deltas in
   order; truncate to `page_count × page_size`; verify `db_sha256`; then
   `PRAGMA integrity_check`; restore records + config from the records item;
   swap with the existing `swap_in_store` gate.
3. On any verification failure: the previous manifest of the same chain, then
   the previous chain, then the newest legacy file. Each fallback is logged
   and reported, never silent.
4. Legacy files restore exactly as today.

Every restore entry point (startup empty-store restore, `mcpbrain restore`,
`restore --auto`, `/api/backup/auto`) uses this one function, so the records
repo and config are restored on every path. (The startup path restored the
store only.)

### 6. Surfaces

- `/api/status` → `backup`: last restore point, outbox items/bytes, current
  upload (file, sent/total), chain and its delta ratio, last error.
- Doctor: `Backup  restore point 2h ago · 2 waiting to upload (24 MB)`; ⚠️ when
  the newest restore point is older than 2 days, or uploads have failed for a
  day, or the outbox exceeds `outbox_max_bytes` (default 2 GB: capture pauses,
  never drops un-uploaded items).
- `mcpbrain backup`: capture now, then follow until the item is committed
  (Ctrl-C leaves it running), with the same progress lines as today.
- `mcpbrain restore --check`: shows which restore point would be used, from
  which location.
- The reflow seed's 24 h backup gate keeps reading `last_backup_success`, now a
  commit.

### 7. Configuration (`backup` block)

New keys with defaults: `capture_interval_s` 3600, `retain_days` 7,
`base_threshold` 2.0 (R37), `upload_budget_s` 60, `outbox_max_bytes` 2_000_000_000.
`interval_s` is ignored for capture (it was the full-upload cadence) and kept
readable for older daemons. `escrow_key`, `shared_drive_id`, `user_id` keep
their meaning; `user_id` only names a legacy folder now.

## Not done, on purpose

- **Local rollback points** (before-images kept on disk) need either a second
  2 GB local copy or before-images of every page, on a disk measured at 90%
  full. Restore from Drive covers the same cases.
- **Fuzzy (piecewise) base capture**: a base is built locally in seconds from
  one consistent copy; only its upload is spread over sessions.
- **Content-addressed dedup / shared segments across chains**: whole-chain
  deletion keeps prune trivially safe.

## Rollout

- Mixed fleet: an old daemon reads only legacy files; a new daemon reads both.
  New daemons keep restoring legacy files forever.
- First run after upgrade: capture is a base (no `pages.idx`), uploaded over as
  many sessions as it takes. Legacy uploads stop.
- Other users' installs start uploading for the first time (the location fix).
  Expect a first full base upload from each.
- Permissions on the backup shared drive are managed by the owner. Prune needs
  delete rights (Content manager) on the person's own folder.
