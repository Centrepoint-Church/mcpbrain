# Incremental Backup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the daily full-snapshot upload with hourly local page-delta captures that upload resumably from an outbox, to one Drive location that every restore path also reads.

**Architecture:** A capture copies the live store with `sqlite3.Connection.backup()`, diffs 8 KiB pages against a local hash index, and writes encrypted page segments plus a sidecar into `app_dir()/backup/outbox/`. A separate time-boxed uploader drains the outbox with Drive's resumable protocol (session persisted to a journal), commits each item by uploading a self-contained manifest, and prunes whole chains. Restore picks the newest restore point across the new layout and every legacy location, rebuilds pages into a staging file, verifies, and swaps.

**Tech Stack:** Python 3.12 stdlib (`sqlite3`, `zlib`, `hashlib`, `tarfile`, `json`), existing `mcpbrain.backup` v3 frame encryption, `google_auth_httplib2.AuthorizedHttp` for raw resumable HTTP, googleapiclient for metadata calls.

**Spec:** `docs/superpowers/specs/2026-10-19-incremental-backup-design.md`

## Global Constraints

- No new dependencies. Compression is `zlib` level 6.
- Page unit = the store's `page_size` (8192 on the live store); never a fixed block size.
- Segment payload ≤ 16 MiB compressed. Upload chunk: start 32 MiB, halve to 8 MiB on failure, regrow after 3 clean chunks; always a multiple of 256 KiB.
- Defaults (`backup` config block): `capture_interval_s` 3600, `retain_days` 7, `base_threshold` 0.5, `upload_budget_s` 60, `outbox_max_bytes` 2_000_000_000. `retain` (old count) is read as days when `retain_days` is absent. `interval_s` no longer drives capture.
- Drive layout: `<drive root>/<person email, lower-case>/chain-<YYYYMMDD>-<6 hex>/{base-NNNN, delta-<seq:06d>-NNNN, records-<seq:06d>, manifest-<chain>-<seq:06d>}`.
- Every file on Drive is addressed by the file id recorded at upload; nothing is deleted or re-uploaded because a listing omitted it.
- Every local state file is written temp + `os.replace`. A sidecar's rename is an item's local commit; a manifest upload is its remote commit.
- `_bulk_lock` is held only for the `backup()` copy, never for upload, prune or restore download.
- Legacy single-file `.enc` snapshots stay restorable forever (v2/v3 frames, bare and tar bundles).
- Every Drive call passes `supportsAllDrives=True`; metadata calls pass `num_retries=backup._NUM_RETRIES` (the retry-convention test scans new modules too — add them to `tests/test_retry_convention.py::_MODULES`).
- Tests never touch the network or the real `MCPBRAIN_HOME`: Drive is a fake, HTTP is a fake transport, home is `tmp_path`.
- Scope test runs to the files a task touches plus directly impacted tests; the owner runs the full suite.

## Review Focus

1. **Lid closed mid-upload, daemon restarted:** the next run resumes the same file from Drive's confirmed offset, not from zero. Test in Task 4 (`test_resume_after_new_process_uses_saved_session`) and Task 5 (`test_upload_step_resumes_item_across_uploader_instances`).
2. **Store swapped or rebuilt under the chain** (restore, `bin/optimise_store.py`, page_size change, page_count shrink): the next capture is a base, never a delta against a state the chain does not hold. Test in Task 3 (`test_store_replaced_forces_base`).
3. **A file a manifest names is gone or corrupt on Drive:** restore falls back to the previous manifest, then the previous chain, then the newest legacy file, and reports which it used. Test in Task 7 (`test_corrupt_delta_falls_back_to_previous_manifest`, `test_broken_chain_falls_back_to_legacy`).
4. **Backups in several locations (the owner's case):** the newest restore point wins by capture time; the June legacy file never beats a newer one. Test in Task 7 (`test_newest_restore_point_wins_across_locations`).
5. **Disk nearly full / outbox full:** capture refuses with a clear `backup_state` error before copying; a full outbox pauses capture and never deletes an un-uploaded item. Test in Task 3 (`test_low_disk_refuses_before_copy`, `test_outbox_full_pauses_capture`).
6. **Drive forgot the upload session** (offline > a week): 404/410 on the status query restarts that file from zero. Test in Task 4 (`test_expired_session_restarts_file`).

---

## File structure

| File | Responsibility |
|---|---|
| `mcpbrain/backup_location.py` (new) | Resolve the one backup location and the legacy folders |
| `mcpbrain/backup_pages.py` (new) | Page index, segment write/read, records tar |
| `mcpbrain/backup_outbox.py` (new) | Capture, base/delta decision, coalescing, outbox accounting |
| `mcpbrain/drive_resumable.py` (new) | Drive resumable upload over raw HTTP, persisted session, adaptive chunks |
| `mcpbrain/backup_uploader.py` (new) | Journal, time-boxed upload step, manifests, commit |
| `mcpbrain/backup_prune.py` (new) | Chain retention and legacy cleanup, resumable deletes |
| `mcpbrain/backup_restore.py` (new) | Candidate discovery, resumable download, rebuild, verify, fallback |
| `mcpbrain/backup_setup.py` | Escrow key named by email; backup config keys |
| `mcpbrain/restore.py` | Entry points delegate to `backup_restore` |
| `mcpbrain/daemon.py` | Capture + upload steps replace `maybe_backup`'s snapshot/upload; startup restore delegates |
| `mcpbrain/control_api.py`, `backup_now.py`, `probes.py`, `doctor.py` | Status, `mcpbrain backup`, doctor lines |
| `docs/RELEASE-RUNBOOK.md`, `CLAUDE.md` | Operator notes |

---

### Task 1: One backup location

**Files:**
- Create: `mcpbrain/backup_location.py`
- Modify: `mcpbrain/backup_setup.py` (escrow key name)
- Test: `tests/test_backup_location.py`, `tests/test_backup_setup.py`

**Interfaces:**
- Produces:
  - `@dataclass(frozen=True) class Location: drive_id: str; person: str; folder_id: str | None; legacy_folders: tuple[str, ...]`
  - `resolve(home: str, drive_service, *, create: bool = False) -> Location`. `drive_id`: `backup.shared_drive_id` when `drives().get` succeeds on it, else the `driveId` of that id as a file (`files().get(fields="driveId")`). `person`: `restore.account_email(home).lower()`; `ValueError` when empty. `folder_id`: the `<drive root>/<person>` folder (created when `create=True`, else None if absent). `legacy_folders`: ids that exist among `<drive root>/<backup.user_id>/` and `<restore._escrow_folder(home)>/<person>/`, de-duplicated, excluding `folder_id`.
  - `backup_setup.enable_backup` writes the escrow key as `<escrow folder>/<person>.key` (person = `account_email`), and stops putting the escrow folder id into `backup.shared_drive_id` (writes the folder's `driveId` instead).

- [ ] **Step 1: Write failing tests** in `tests/test_backup_location.py` with a fake Drive (`drives().get`, `files().get`, `files().list`, `files().create`):
  - `test_drive_id_used_as_is` — `shared_drive_id="0Adrive"` that `drives().get` accepts → `loc.drive_id == "0Adrive"`.
  - `test_folder_id_resolves_to_its_drive` — `shared_drive_id="1folder"`, `drives().get` raises 404, `files().get` returns `{"driveId": "0Adrive"}` → `loc.drive_id == "0Adrive"`.
  - `test_person_is_lowercased_email` — owner_email `Dana.Okafor@Example.org` → `loc.person == "dana.okafor@example.org"`.
  - `test_legacy_folders_found_and_deduplicated` — fake holds `<root>/dana-okafor` and `<escrow>/dana.okafor@…` → both ids in `legacy_folders`, in that order.
  - `test_create_makes_person_folder_once` — `create=True` twice → exactly one `files().create` with `parents=[drive_id]`, `name=person`.
  - `test_no_email_raises`.
- [ ] **Step 2:** Run `uv run pytest tests/test_backup_location.py -q` → FAIL (module missing).
- [ ] **Step 3:** Implement `mcpbrain/backup_location.py`. Lists use `corpora="drive", driveId=drive_id` for the root and `corpora="allDrives"` for the escrow folder; folder query `name = '<n>' and mimeType = folder and trashed = false and '<parent>' in parents` (reuse `backup._guard_user_id` on names).
- [ ] **Step 4:** Add `test_enable_backup_names_key_by_email` and `test_enable_backup_stores_drive_id_not_folder_id` to `tests/test_backup_setup.py`; change `enable_backup` accordingly (user_id stays as written today for legacy-folder discovery).
- [ ] **Step 5:** Run `uv run pytest tests/test_backup_location.py tests/test_backup_setup.py tests/test_control_api_backup_enable.py -q` → PASS.
- [ ] **Step 6:** Commit `feat(backup): one backup location resolver; escrow key named by email`.

### Task 2: Page segments and page index

**Files:**
- Create: `mcpbrain/backup_pages.py`
- Test: `tests/test_backup_pages.py`

**Interfaces:**
- Produces:
  - `SEGMENT_MAX_BYTES = 16 * 1024 * 1024`
  - `class PageIndex` with `load(path) -> PageIndex` (missing file → empty), `save(path)` (atomic), `__len__`, `get(pgno) -> bytes | None`, `set(pgno, digest)`, `truncate(page_count)`. Stored as concatenated 16-byte blake2b digests, page 1 first.
  - `def page_digest(page: bytes) -> bytes` — `hashlib.blake2b(page, digest_size=16).digest()`.
  - `def scan_changes(db_path, index: PageIndex) -> ScanResult` where `@dataclass ScanResult: page_size: int; page_count: int; changed: list[int]; db_sha256: str; new_index: PageIndex`. Reads `page_size` from the header (bytes 16–17, value 1 means 65536); streams the file once.
  - `def write_segments(db_path, pgnos, *, out_dir, prefix, key, header: dict) -> list[SegmentFile]` where `@dataclass SegmentFile: name: str; path: Path; size: int; sha256: str; md5: str; first_pgno: int; last_pgno: int; pages: int`. Payload = `b"MCPBRAIN-PAGES-1\n" + json(header + first/last pgno) + b"\n"` followed by zlib(level 6) of `>I`-pgno + page records; a new segment starts when the compressed payload would pass `SEGMENT_MAX_BYTES`. Encrypted with `backup.open_encrypted(path, key)`. Files are named `<prefix>-0001`, `-0002`, … and written to `<name>.tmp` then renamed.
  - `def read_segment(path, key) -> tuple[dict, Iterator[tuple[int, bytes]]]` — header and (pgno, page) pairs; `ValueError` on a bad magic.
  - `def records_tar(records_dir, config_path, out_path, key) -> tuple[str, int]` — encrypted tar (arc names `records/…`, `config.json`, same as `backup._RECORDS_ARC`/`_CONFIG_ARC`), returns (sha256 of ciphertext, size). `def records_fingerprint(records_dir, config_path) -> str` — sha256 over sorted `(relpath, size, sha256)` of every file.

- [ ] **Step 1: Write failing tests:**
  - `test_scan_on_empty_index_reports_every_page` — 50-page sqlite file → `changed == list(range(1, 51))`, `page_count == 50`.
  - `test_scan_reports_only_rewritten_pages` — rewrite bytes of page 7 and 30 → `changed == [7, 30]`.
  - `test_scan_handles_shrink_and_growth` — `page_count` follows the file; `new_index` length equals it.
  - `test_db_sha256_matches_hashlib`.
  - `test_segments_round_trip_pages` — write pages [1..50], `read_segment` yields identical bytes per pgno.
  - `test_segments_split_at_max_bytes` — monkeypatch `SEGMENT_MAX_BYTES` small → >1 file, contiguous pgno ranges, names `-0001`, `-0002`.
  - `test_segment_wrong_key_raises` (InvalidToken).
  - `test_index_save_is_atomic` — save, corrupt nothing on failure (monkeypatch `os.replace` to raise → old file intact).
  - `test_records_fingerprint_changes_with_content_not_mtime`.
- [ ] **Step 2:** Run `uv run pytest tests/test_backup_pages.py -q` → FAIL.
- [ ] **Step 3:** Implement `mcpbrain/backup_pages.py`.
- [ ] **Step 4:** Run the tests → PASS.
- [ ] **Step 5:** Commit `feat(backup): page index and encrypted page segments`.

### Task 3: Capture into the outbox

**Files:**
- Create: `mcpbrain/backup_outbox.py`
- Test: `tests/test_backup_outbox.py`

**Interfaces:**
- Consumes: Task 2.
- Produces:
  - `@dataclass Item: name: str; kind: Literal["base", "delta"]; chain: str; seq: int; prev_seq: int | None; captured_at: float; page_size: int; page_count: int; db_sha256: str; segments: list[dict]; records: dict | None; bytes: int` with `to_json()/from_json()`. Sidecar file `<outbox>/<chain>-<seq:06d>.json`.
  - `@dataclass CaptureState` persisted at `<backup dir>/capture.json`: `chain, seq, page_size, page_count, db_sha256, records_fp, base_bytes, delta_bytes, store_id`.
  - `def backup_dir(home) -> Path` = `Path(home) / "backup"`; outbox = `backup_dir/"outbox"`, work = `backup_dir/"work"`.
  - `def capture(home, store_path, key, *, lock=nullcontext(), settings: Settings, clock=time.time) -> Item | None`. `Settings` dataclass holds the Global-Constraints defaults and is built by `Settings.from_config(cfg_backup: dict)`. Returns None when nothing changed (no pages, same records fingerprint). Raises `OutboxFull` when `outbox_bytes(home) >= outbox_max_bytes`; raises `OSError(ENOSPC)` before copying when free space in work dir < `page_count × page_size × 1.1`.
    1. sweep `*.tmp` and segment files with no sidecar in outbox/work.
    2. under `lock`: `sqlite3.connect(f"file:{store_path}?mode=ro", uri=True).backup(dst)` into `work/capture.sqlite3`.
    3. decide base vs delta (base when: no CaptureState; `page_size` differs; `store_id` differs; `pages.idx` missing; `delta_bytes > base_threshold × base_bytes`). `store_id` = `f"{st.st_dev}:{st.st_ino}"` of the store file: `swap_in_store` and `bin/optimise_store.py --swap` both replace the file with `os.replace`, which changes the inode, so a swapped or rebuilt store always starts a new chain.
    4. scan, write segments (prefix `base` or `delta-<seq:06d>`), records tar when the fingerprint changed (`records-<seq:06d>`), then sidecar (tmp+rename), then `pages.idx`, then `capture.json`; delete the copy in `finally`.
  - `def coalesce(home, key, *, started: set[str]) -> Item | None` — merges ≥2 consecutive delta items of one chain whose names are not in `started` into one item (newest page wins: read older segments, overlay newer, rewrite segments with prefix `delta-<newest seq:06d>`; `prev_seq` = oldest's `prev_seq`; records = newest non-None). Writes the merged sidecar, then deletes the merged-away sidecars and segments.
  - `def pending_items(home) -> list[Item]` (sorted by chain capture order, then seq), `def outbox_bytes(home) -> int`, `class OutboxFull(Exception)`.

- [ ] **Step 1: Write failing tests** (real tiny sqlite stores in `tmp_path`):
  - `test_first_capture_is_base_with_every_page`.
  - `test_second_capture_is_delta_with_only_changed_pages` — insert a row between captures; delta pages ⊂ all pages, `prev_seq == 1`, `seq == 2`.
  - `test_no_change_returns_none`.
  - `test_records_item_only_when_fingerprint_changes`.
  - `test_store_replaced_forces_base` — `os.replace` a copy over the store between captures → kind `base`, new chain id.
  - `test_delta_bytes_over_threshold_forces_base` — `base_threshold=0.0` → second capture is base.
  - `test_crash_before_index_save_redoes_capture` — monkeypatch `PageIndex.save` to raise once; next capture produces a delta with the same pages, and the orphan from the failed run is swept.
  - `test_low_disk_refuses_before_copy` — monkeypatch `shutil.disk_usage` → `OSError` with `errno.ENOSPC`, no file in work.
  - `test_outbox_full_pauses_capture` — `outbox_max_bytes=1` with one pending item → `OutboxFull`, pending item untouched.
  - `test_coalesce_newest_page_wins` — three deltas touching the same page; merged item rebuilds to the third version; `started={second}` leaves the second alone and merges nothing across it.
  - `test_copy_holds_lock_only_for_backup` — `lock` records enter/exit around the `backup()` call only.
- [ ] **Step 2:** Run `uv run pytest tests/test_backup_outbox.py -q` → FAIL.
- [ ] **Step 3:** Implement `mcpbrain/backup_outbox.py`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(backup): local page-delta capture into an outbox`.

### Task 4: Resumable Drive upload with a persisted session

**Files:**
- Create: `mcpbrain/drive_resumable.py`
- Test: `tests/test_drive_resumable.py`

**Interfaces:**
- Produces:
  - `class Transport(Protocol): def request(self, uri: str, method: str, body: bytes | None, headers: dict) -> tuple[int, dict, bytes]`. `def authorized_transport(creds, timeout_s=auth.DEFAULT_HTTP_TIMEOUT_S) -> Transport` wraps `AuthorizedHttp(creds, http=auth._google_http(timeout_s))`.
  - `@dataclass Session: uri: str; offset: int; chunk: int; clean: int` with `to_json/from_json`.
  - `def upload_file(transport, path, *, name, parent_id, session: Session | None, save: Callable[[Session], None], deadline: float, clock=time.monotonic, sleep=time.sleep) -> dict | None`. Starts a session (`POST https://www.googleapis.com/upload/drive/v3/files?uploadType=resumable&supportsAllDrives=true&fields=id,size,md5Checksum`, JSON metadata `{"name", "parents": [parent_id]}`, header `X-Upload-Content-Length`) when `session` is None; on an existing session first asks `PUT` with `Content-Range: bytes */<size>` and continues from the returned `Range` (none → 0). Sends `PUT` chunks with `Content-Range: bytes a-b/size`; 308 → `offset = Range end + 1`, `save(session)`; 200/201 → returns the JSON (`id`, `size`, `md5Checksum`). Returns None when `clock() >= deadline` between chunks (session saved). 404/410 on the session → new session from zero. Transient failure (exception classified by `backup._is_transient_upload_error`, or 5xx/429 status) → `chunk = max(8 MiB, chunk // 2)`, `clean = 0`, sleep `5 × 2**k` capped at 240 s, retry; 6 consecutive failures without progress → raise. 3 clean chunks → `chunk = min(32 MiB, chunk * 2)`.
  - `def verify(meta: dict, path) -> None` — raises `UploadMismatch` unless `int(meta["size"])` equals the file size and `meta["md5Checksum"]` equals the local MD5.

- [ ] **Step 1: Write failing tests** with a fake transport that simulates Drive's protocol in memory (stores received bytes per session, supports `bytes */size` queries, can be scripted to raise or to return 503/404):
  - `test_uploads_in_chunks_and_returns_metadata` — 70 MiB file → 3 PUTs (32+32+6), returned md5 equals local.
  - `test_deadline_stops_between_chunks_and_saves_session` — deadline hit after first chunk → returns None, saved `offset == 32 MiB`.
  - `test_resume_after_new_process_uses_saved_session` — second call with the saved Session and a fresh transport bound to the same fake server → first request is the `bytes */size` query, total bytes sent across both calls == file size.
  - `test_dropped_chunk_resumes_from_server_offset` — transport raises `TimeoutError` mid-chunk once after the server stored half of it → completes, server bytes equal file.
  - `test_chunk_halves_on_failure_and_regrows` — sequence of chunk sizes recorded: 32, (fail) 16, 16, 16, 16, 32 MiB.
  - `test_expired_session_restarts_file` — status query returns 404 → new POST, upload from 0.
  - `test_permanent_403_raises_immediately`.
  - `test_verify_rejects_size_or_md5_mismatch`.
- [ ] **Step 2:** Run `uv run pytest tests/test_drive_resumable.py -q` → FAIL.
- [ ] **Step 3:** Implement `mcpbrain/drive_resumable.py`.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(backup): Drive resumable upload with a persisted session`.

### Task 5: Uploader: journal, manifests, commit

**Files:**
- Create: `mcpbrain/backup_uploader.py`
- Test: `tests/test_backup_uploader.py`

**Interfaces:**
- Consumes: Task 1 `resolve`, Task 3 `pending_items/coalesce/Item`, Task 4 `upload_file/verify/Session`, `daemon.write_backup_state` (move it to `mcpbrain/backup_state.py` as `write_backup_state(home, *, ok, error=None, restore_point=None)` and re-export from `daemon` for existing callers).
- Produces:
  - Journal at `<backup dir>/journal.json`: `{"chains": {chain: {"folder_id", "committed_seq", "files": [ {name, file_id, size, sha256, kind, seq} ], "base_bytes", "first_captured_at", "last_captured_at"}}, "inflight": {item_name: {file_name: Session json | {"file_id": …}}}, "pending_deletes": [file_id, …]}`. `load_journal(home) -> dict`, `save_journal(home, j)` (atomic).
  - `@dataclass StepResult: committed: list[str]; progress: dict | None; error: str | None; pending: int; pending_bytes: int`.
  - `def upload_step(home, *, drive_service, transport, key, budget_s, clock=time.monotonic, wall=time.time, on_progress=None) -> StepResult`. Order: resolve location (`create=True`); `coalesce` with `started` = items in `journal["inflight"]`; for the oldest item: ensure chain folder `chain-<chain>` under the person folder (`files().create` once, id stored in journal); upload each segment and records file not yet done (`upload_file` with the saved Session; `verify`; record file id; delete the local file); when all done, build the manifest (`format: 1`, chain, seq, captured_at, page_size, page_count, db_sha256, `files`: every file of the chain's committed items + this item, in apply order, newest records file only) as encrypted JSON (`backup.open_encrypted`) and upload it with a plain `files().create(media_body=MediaInMemoryUpload)`, verify md5; then update `committed_seq`, delete the sidecar, `write_backup_state(ok=True, restore_point=item.captured_at)`. Continues to the next item while budget remains.
  - Failure → `write_backup_state(ok=False, error=…)` once per step, `StepResult.error` set, nothing local deleted.
  - Chain continuity: before uploading an item, require `item.prev_seq == chains[item.chain].committed_seq` (a base needs no predecessor). Otherwise the item and every later item of that chain are discarded locally, `capture.json` is deleted (so the next capture is a base), and the step logs the gap once.
- [ ] **Step 1: Write failing tests** with the Task 4 fake server and a fake Drive (`files().create/get/list`):
  - `test_commits_base_then_delta_with_self_contained_manifests` — decrypt manifest 2 → lists base segments then delta segments then records, file ids match uploads.
  - `test_upload_step_resumes_item_across_uploader_instances` — budget expires mid-file; a new call (fresh objects, same home) finishes it without re-sending confirmed bytes.
  - `test_manifest_only_after_every_file_verified` — md5 mismatch on one segment → no manifest uploaded, item still pending, error recorded.
  - `test_coalesces_before_upload_but_not_started_items`.
  - `test_backup_state_records_restore_point_on_commit`.
  - `test_new_chain_base_uploads_into_new_folder`.
  - `test_failure_keeps_local_files`.
  - `test_gap_in_chain_forces_new_base` — delete a pending delta's sidecar by hand; the next item is discarded, `capture.json` is gone, the next capture is a base.
- [ ] **Step 2:** Run `uv run pytest tests/test_backup_uploader.py -q` → FAIL.
- [ ] **Step 3:** Implement `mcpbrain/backup_state.py` (move) and `mcpbrain/backup_uploader.py`.
- [ ] **Step 4:** Run the new tests plus `tests/test_daemon.py -k backup_state` → PASS.
- [ ] **Step 5:** Commit `feat(backup): time-boxed outbox uploader with manifest commits`.

### Task 6: Prune whole chains and legacy files

**Files:**
- Create: `mcpbrain/backup_prune.py`
- Test: `tests/test_backup_prune.py`

**Interfaces:**
- Consumes: Task 1, Task 5 journal.
- Produces: `def prune_step(home, *, drive_service, retain_days: float, now: float, budget_s: float, clock=time.monotonic) -> dict` returning `{"deleted": [...], "held": dict | None}`.
  - Chains: keep the newest chain always; keep any chain with `last_captured_at >= now − retain_days×86400`; an older chain is eligible only when a newer chain has `committed_seq >= 1`. Eligible chain folder ids are appended to `journal["pending_deletes"]`, then deleted one per call to `files().delete(fileId, supportsAllDrives=True)` while budget remains; a 404 counts as done.
  - Hold: if the newest chain's `base_bytes < 0.5 ×` the previous chain's `base_bytes`, return `held={"newest": …, "previous": …}` and delete nothing.
  - Legacy: once the oldest committed restore point in the new layout is older than `retain_days`, list `.enc` files in `Location.legacy_folders` and queue them for deletion the same way.
- [ ] **Step 1: Write failing tests:**
  - `test_keeps_newest_chain_even_if_old`.
  - `test_deletes_expired_chain_only_after_successor_committed`.
  - `test_interrupted_prune_continues_from_pending_list`.
  - `test_small_new_base_holds_prune`.
  - `test_legacy_files_kept_until_new_layout_covers_retention`.
  - `test_404_on_delete_is_done`.
- [ ] **Step 2:** Run `uv run pytest tests/test_backup_prune.py -q` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(backup): stepwise chain and legacy prune`.

### Task 7: Restore: newest across locations, resumable, verified

**Files:**
- Create: `mcpbrain/backup_restore.py`
- Test: `tests/test_backup_restore.py`

**Interfaces:**
- Consumes: Tasks 1, 2, 5 (manifest format), `backup.decrypt_file/restore/swap_in_store`.
- Produces:
  - `@dataclass Candidate: kind: Literal["manifest", "legacy"]; captured_at: float; file_id: str; folder_id: str; label: str`.
  - `def candidates(home, drive_service, key) -> list[Candidate]` — newest first. Manifests: list `manifest-*` in every `chain-*` folder of `Location.folder_id`, decrypt each chain's newest few to read `captured_at` (cache by file id under `<backup dir>/restore/`). Legacy: `.enc` files in `legacy_folders` and in `folder_id` itself; `captured_at` = createdTime.
  - `def restore_latest(home, drive_service, key, *, dest_store, records_dir=None, config_path=None, swap=None, budget_s=None) -> RestoreResult` where `@dataclass RestoreResult: used: Candidate; fallbacks: list[tuple[Candidate, str]]; path: Path`. Tries candidates in order. Manifest path: download each named file with `files().get_media` + `MediaIoBaseDownload` to `<backup dir>/restore/<file_id>` (skip files already present with the recorded sha256 — that is the resume), verify sha256, decrypt segments, write pages into `<dest_store>.stage` at `(pgno−1)×page_size`, truncate to `page_count×page_size`, verify `db_sha256`, `PRAGMA integrity_check == "ok"`, extract the records file into `records_dir`/`config_path` (same rules as `backup.restore`), then `swap(stage, dest_store)` (default `backup.swap_in_store`). Legacy path: `backup.download_snapshot` + `backup.restore` + swap, exactly today's behaviour. Any failure → record `(candidate, reason)` and try the next older candidate (the previous manifest of the same chain comes before the previous chain because candidates are newest-first). Raises `NoRestorePoint` when all fail.
- [ ] **Step 1: Write failing tests** (build a real chain with Tasks 2–5 against the fake Drive):
  - `test_rebuilds_byte_identical_store_from_base_and_deltas`.
  - `test_newest_restore_point_wins_across_locations` — legacy file createdTime 2026-06-16 in `<escrow>/<person>/`, chain manifest captured 2026-10-19 → manifest used; and vice versa when the legacy file is newer.
  - `test_corrupt_delta_falls_back_to_previous_manifest` — flip a byte in the newest delta → result.used is seq−1, `fallbacks` names the newest with a sha256 reason.
  - `test_broken_chain_falls_back_to_legacy` — delete the base file from the fake → legacy used.
  - `test_download_resumes_from_files_already_verified` — interrupt after two files; second call downloads only the rest.
  - `test_records_and_config_restored_on_every_path`.
  - `test_never_touches_dest_until_verified` — failing `integrity_check` leaves the original dest bytes unchanged.
- [ ] **Step 2:** Run `uv run pytest tests/test_backup_restore.py -q` → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run → PASS.
- [ ] **Step 5:** Commit `feat(backup): restore the newest restore point across locations`.

### Task 8: Every restore entry point uses `restore_latest`

**Files:**
- Modify: `mcpbrain/restore.py` (`run_restore`, `run_restore_auto`, `detect_restorable`, `--check`), `mcpbrain/daemon.py` (`_restore_on_first_run_classified`, `_probe_snapshot`), `mcpbrain/control_api.py` (`/api/backup/auto`)
- Test: `tests/test_restore_auto.py`, `tests/test_restore_first_run.py`, `tests/test_restore_daemon_guard.py`, `tests/test_control_api_post.py`

**Interfaces:**
- Consumes: Task 7.
- Produces: `detect_restorable(home, drive_service)` keeps its return keys and adds `restore_point` (ISO time) and `source` (`"chain"` / `"legacy:<folder name>"`); `snapshot_id` is the chosen candidate's file id. `mcpbrain restore --check` prints the chosen restore point, its source and age. Startup empty-store restore passes `records_dir`/`config_path` (it restored the store only).
- [ ] **Step 1:** Update the existing tests that monkeypatch `find_latest_snapshot` / `find_latest_in_subfolder` / `download_snapshot` to patch `backup_restore.candidates` / `restore_latest` instead, and add `test_auto_restore_reports_source_and_age` and `test_startup_restore_restores_records_and_config`.
- [ ] **Step 2:** Run those test files → FAIL.
- [ ] **Step 3:** Rewire the entry points; keep `refuse_if_daemon_running`, the restore hold, `RestoreBusyError` handling and the transient/permanent classification exactly as they are.
- [ ] **Step 4:** Run the four test files plus `tests/test_daemon_google_retry.py` → PASS.
- [ ] **Step 5:** Commit `refactor(restore): every entry point restores the newest point via backup_restore`.

### Task 9: Daemon: capture and upload replace the full snapshot

**Files:**
- Modify: `mcpbrain/daemon.py` (`BackupConfig`, `_backup_from_config`, `maybe_backup`, `_backup_under_bulk_lock`, `request_backup`, `status`)
- Test: `tests/test_daemon.py`, `tests/test_daemon_google_retry.py`, `tests/test_backup_wiring.py`

**Interfaces:**
- Consumes: Tasks 3, 5, 6.
- Produces:
  - `BackupConfig` gains `settings: backup_outbox.Settings` and `transport: object | None` (built from the same credentials as `drive_service` via `drive_resumable.authorized_transport`); `out_path` is removed.
  - `maybe_backup()` = **capture** when due (`settings.capture_interval_s`, the failure backoff from `BACKUP_RETRY_BASE_S`, `request_backup`, dark-wake deferral, restore-pending skip all unchanged) with `lock=self._bulk_lock` passed into `backup_outbox.capture`, i.e. the lock is taken inside capture for the copy only. `_backup_under_bulk_lock` no longer takes the lock.
  - New `upload_backup_step()` runs every loop iteration after capture: `backup_uploader.upload_step(budget_s=settings.upload_budget_s)` then `backup_prune.prune_step` with the remaining budget; `_note_progress("backup")` from `on_progress`; sets `self._backup_progress` (`{"phase": "upload", "sent", "total", "item"}`) and `self._prune_held`.
  - `status()["backup"] = {"restore_point", "pending", "pending_bytes", "uploading", "chain", "delta_ratio", "last_error"}`; `backup_running` keeps its shape for `mcpbrain backup` (phase `capture` | `upload`).
  - `_build_drive_service` failure handling, `write_backup_state`, `_backup_failures` and `backup_failures()` keep working; a failed capture or a failed upload step each count as a failure.
- [ ] **Step 1:** Rewrite the backup tests in `tests/test_daemon.py` that assert `make_encrypted_snapshot`/`upload_snapshot` calls to assert capture/upload/prune calls, and add:
  - `test_capture_takes_bulk_lock_only_for_copy` — a second thread can acquire `_bulk_lock` while `upload_backup_step` runs.
  - `test_request_backup_captures_now_and_uploads_until_committed`.
  - `test_upload_step_runs_every_iteration_even_when_capture_not_due`.
  - `test_status_backup_block_shape`.
- [ ] **Step 2:** Run `uv run pytest tests/test_daemon.py tests/test_daemon_google_retry.py tests/test_backup_wiring.py tests/test_daemon_watchdog.py tests/test_bulk_lock_fairness.py -q` → FAIL.
- [ ] **Step 3:** Implement. Delete the now-unused full-snapshot path from the daemon (`make_encrypted_snapshot`/`upload_snapshot`/`prune_snapshots` stay in `backup.py` for `bin/optimise_store.py` and legacy tests).
- [ ] **Step 4:** Run the same command → PASS.
- [ ] **Step 5:** Commit `feat(daemon): hourly page-delta capture with a resumable outbox upload`.

### Task 10: Surfaces: `mcpbrain backup`, doctor, probe

**Files:**
- Modify: `mcpbrain/backup_now.py`, `mcpbrain/probes.py` (`probe_backup`, `last_backup_success`), `mcpbrain/doctor.py`
- Test: `tests/test_backup_now.py`, `tests/test_probes.py`, `tests/test_doctor*.py`

**Interfaces:**
- Consumes: Task 9 status block; `backup_state.json` gains `restore_point`.
- Produces:
  - `mcpbrain backup`: prints `Capturing changes…`, then `Uploading: <sent> of <total> MB (<pct>%) · <n> waiting`, then `✅ Backed up. Restore point <HH:MM>.` when `backup_state.json` `last_success` advances past the request and `pending == 0`.
  - `probe_backup` reads `restore_point` (falls back to `last_success`); healthy within 2 days. Doctor line: `Backup  restore point 2h ago · 2 waiting to upload (24 MB)`; ⚠️ when restore point > 2 days old, uploads failing ≥ 1 day (`consecutive_failures` with `last_success` > 24 h), or outbox ≥ `outbox_max_bytes`. Remedy stays `Run: mcpbrain backup`.
- [ ] **Step 1:** Write failing tests: `test_backup_now_waits_for_pending_zero`, `test_probe_uses_restore_point`, `test_doctor_backup_line_shows_waiting_uploads`, `test_doctor_warns_when_outbox_full`.
- [ ] **Step 2:** Run → FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest tests/test_backup_now.py tests/test_probes.py tests/test_probes_cache.py tests/test_doctor*.py tests/test_reflow_seed.py -q` → PASS.
- [ ] **Step 5:** Commit `feat(backup): status, doctor and mcpbrain backup for the outbox`.

### Task 11: Operator docs and live verification

**Files:**
- Modify: `docs/RELEASE-RUNBOOK.md` (backup/restore sections), `CLAUDE.md` (a short "Incremental backup" section: layout, location fix, what was verified live)
- Test: none new; `uv run ruff check mcpbrain tests`, and the impacted suites from Tasks 1–10.

- [ ] **Step 1:** Update the docs.
- [ ] **Step 2:** Local install per CLAUDE.md (bootout → `uv tool install --reinstall --force ".[daemon]"` → clear `__pycache__` → bootstrap); confirm the installed files contain `backup_outbox.py` and `upload_step`.
- [ ] **Step 3:** `mcpbrain backup` → base captured and committed; record outbox size, upload time, Drive layout (`<root>/dana.okafor@example.org/chain-…/`).
- [ ] **Step 4:** Wait for the next hourly capture (or `mcpbrain backup` again) → a delta committed; record its size.
- [ ] **Step 5:** Kill the daemon mid-upload of a base or delta (`launchctl kickstart -k`), confirm the resumed upload sends only the remainder (journal offset before/after).
- [ ] **Step 6:** `mcpbrain restore --check` names the new chain, not the 16 June legacy file. Restore into a scratch path with `restore_latest(..., dest_store=<scratch>, swap=lambda s, d: os.replace(s, d))` and confirm `integrity_check` ok and the db sha256 equals the manifest's.
- [ ] **Step 7:** Commit `docs: incremental backup operator notes` and push.
