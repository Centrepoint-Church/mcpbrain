# Ingestion Resilience + Tabular Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close five independent ingestion-resilience gaps: a codebase-wide Drive/Calendar API retry gap, a spreadsheet-rendering bug that produces 293KB chunks, unbounded consolidated-note writes, Drive-only re-chunk repair tooling, and empty prior-message context for short email threads.

**Architecture:** Fifteen self-contained tasks across five phases, each independently testable and committable. Phase order matters only where the spec requires it (Phase 2 must land before Phase 3's `CHUNKER_VERSION` bump); phases 1, 4, and 5 have no dependencies on anything else in this plan and can run in any order.

**Tech Stack:** Python 3.12, pytest (`uv run pytest`), sqlite3, `googleapiclient` (Google Drive/Calendar API client).

**Spec:** `docs/superpowers/specs/2026-08-24-ingestion-resilience-and-tabular-rendering-design.md`

## Global Constraints

- Run `uv run pytest <changed test file(s)> -q` after every task's implementation step — do not run the full suite per task (that's a final gate, not a per-task one).
- Run `uv run ruff check <changed file(s)>` before each commit.
- Every new/changed public function keeps this codebase's existing docstring convention: state the mechanism and the *why*, not just the *what* — see any function referenced in this plan for the house style.
- Never touch `mcpbrain/backup.py`'s `_MEDIA_NUM_RETRIES = 0` constant or its call site — it is a deliberate, already-correct exception (a retried chunk of the resumable media upload can't re-seek a non-seekable stream). Every task below that adds retries explicitly excludes it.
- Commit after every task (not every step) using the repo's normal commit style — see recent `git log` for tone. Do not push until the final task's gate at the end of this plan.

---

## Phase 1 — Retry gap: `num_retries` codebase-wide (spec §1)

### Task 1: `backup.py` — add `_NUM_RETRIES` to every non-media call, including `prune_snapshots`

**Files:**
- Modify: `mcpbrain/backup.py` (module-level constant + ~13 call sites)
- Modify: `tests/test_backup.py` (`_FakeList`, `_FakeDelete` fixtures)
- Test: `tests/test_backup.py`

**Interfaces:**
- Produces: `mcpbrain.backup._NUM_RETRIES = 5` (importable by tests), alongside the existing `_MEDIA_NUM_RETRIES = 0` (unchanged).

**Context:** `backup.py` already has `_MEDIA_NUM_RETRIES = 0` for the one resumable media-upload call, well-commented as deliberate. Every *other* `.execute()` call in the file (the folder lookup/create in `upload_snapshot`, `find_latest_snapshot`, all of `prune_snapshots`, `_list_in_drives`, `ensure_subfolder`) has no retry at all — these are plain metadata list/create/delete calls or in-memory-body uploads, none of which have the "can't re-seek" problem, so they're all safe to retry. The test fixtures `_FakeList.execute(self)` and (for the delete path) `_FakeDelete.execute(self, num_retries=0)` need to accept/record `num_retries` too, or adding the kwarg to production code breaks every existing test that uses `_FakeList`.

- [ ] **Step 1: Update the test fixtures to accept and record `num_retries`**

In `tests/test_backup.py`, find the `_FakeList` class (around line 1091) and give it the same recording shape `_FakeCreate` already has:

```python
class _FakeList:
    def __init__(self, calls, canned, executes=None):
        self.calls = calls
        self.canned = canned
        self.executes = executes if executes is not None else []

    def execute(self, num_retries=0):
        self.executes.append(num_retries)
        return self.canned
```

Update every constructor call that builds a `_FakeList` to keep working with the new optional `executes` param (no call site changes needed — it defaults to a fresh list). `FakeFiles.list()` (around line 1119) currently does `return _FakeList(self.list_calls, self.list_response)` — leave it as-is; the new `executes` param is optional. Also update `_FakeDelete` (around line 1863) — it already accepts `num_retries=0`, just add recording:

```python
class _FakeDelete:
    def __init__(self, deleted, file_id, executes=None):
        self.deleted = deleted
        self.file_id = file_id
        self.executes = executes if executes is not None else []

    def execute(self, num_retries=0):
        self.executes.append(num_retries)
        self.deleted.append(self.file_id)
        return {}
```

And `FakeFilesPrune.delete()` (around line 1885) to pass a shared list through if you want to assert on it later:

```python
    def __init__(self, snapshot_files):
        self._snaps = snapshot_files
        self.deleted = []
        self.delete_retries = []

    def delete(self, *, fileId, supportsAllDrives=False):
        return _FakeDelete(self.deleted, fileId, self.delete_retries)
```

- [ ] **Step 2: Write the failing tests**

Add to `tests/test_backup.py`:

```python
def test_upload_snapshot_folder_lookup_and_create_pass_num_retries(tmp_path):
    from mcpbrain.backup import upload_snapshot, _NUM_RETRIES

    src = tmp_path / "snap.enc"
    src.write_bytes(b"ciphertext")
    files = FakeFiles(list_response={"files": []})
    service = FakeService(files)

    upload_snapshot(service, src, "drive-XYZ", "sam", media_factory=_fake_media)

    # Folder lookup used _FakeList (no create), the two create() calls used
    # _FakeCreate — both fakes now record num_retries via `files.execute_retries`.
    assert files.execute_retries == [_NUM_RETRIES, _NUM_RETRIES]


def test_prune_snapshots_list_and_delete_pass_num_retries():
    from mcpbrain.backup import prune_snapshots, _NUM_RETRIES

    files = [_snap(i, i) for i in range(1, 6)]
    fake_files = FakeFilesPrune(files)
    svc = FakeService(fake_files)

    prune_snapshots(svc, "drive-X", "sam", keep=3)

    assert fake_files.delete_retries == [_NUM_RETRIES, _NUM_RETRIES]
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `uv run pytest tests/test_backup.py -k "num_retries" -v`
Expected: `FAIL` — `AttributeError: module 'mcpbrain.backup' has no attribute '_NUM_RETRIES'` (or an empty/zero `execute_retries` assertion failure once the fixtures compile).

- [ ] **Step 4: Add `_NUM_RETRIES` and apply it to every non-media call**

In `mcpbrain/backup.py`, near the existing `_MEDIA_NUM_RETRIES = 0` (around line 744), add:

```python
# Every OTHER Drive call in this module is a plain metadata list/create/delete
# or an in-memory-body upload — none has _MEDIA_NUM_RETRIES' "can't re-seek a
# stream" problem, so all of them retry. googleapiclient's own num_retries
# param already does randomized exponential backoff on exactly this error
# class (SSL errors, socket timeouts, ConnectionError, OSError generally —
# confirmed against googleapiclient.http._retry_request), so no new retry
# logic is written here, just the parameter every other call in this codebase
# that already retries (sync/gmail.py, sync/attachments.py) also passes.
_NUM_RETRIES = 5
```

Add `num_retries=_NUM_RETRIES` to every `.execute()` call in the file EXCEPT the one using `_MEDIA_NUM_RETRIES` (search the file for `.execute()` with no argument — there are ~13, spanning `upload_snapshot`'s folder lookup/create, `find_latest_snapshot`'s two list calls, `prune_snapshots`'s two list calls and one delete call, `_list_in_drives`, and `ensure_subfolder`'s create). Example transformation (the folder lookup in `upload_snapshot`):

```python
    resp = (
        service.files()
        .list(
            q=q,
            corpora="drive",
            driveId=shared_drive_id,
            includeItemsFromAllDrives=True,
            supportsAllDrives=True,
            fields="files(id, name)",
        )
        .execute(num_retries=_NUM_RETRIES)
    )
```

Apply the identical `.execute(num_retries=_NUM_RETRIES)` pattern to the other ~12 sites. `prune_snapshots`'s delete call becomes:

```python
            service.files().delete(fileId=f["id"], supportsAllDrives=True) \
                .execute(num_retries=_NUM_RETRIES)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_backup.py -q`
Expected: `PASS` (full file, not just the new tests — confirms the fixture change didn't break anything else).

- [ ] **Step 6: Ruff and commit**

```bash
uv run ruff check mcpbrain/backup.py tests/test_backup.py
git add mcpbrain/backup.py tests/test_backup.py
git commit -m "fix(backup): retry every non-media Drive call, not just uploads

prune_snapshots ran right after every backup upload with zero retry --
equally exposed to the Errno-49-class transient failure that motivated this
fix, but missed by a backup-only patch. googleapiclient's own num_retries
already does exponential backoff on exactly this error class."
```

---

### Task 2: `fleet.py` — same treatment

**Files:**
- Modify: `mcpbrain/fleet.py` (module-level constant + 5 call sites)
- Test: `tests/test_fleet.py`

**Interfaces:**
- Produces: `mcpbrain.fleet._NUM_RETRIES = 5`.

**Context:** `_list_all` (the `.list()` in its `while True` loop), `_upload_text`'s `update()`/`create()` calls (both use `MediaInMemoryUpload` — an in-memory byte buffer, not a resumable stream, so retrying is safe: a retry just resends the same bytes), and two bare `.execute()` calls at `get_media`/`delete` call sites (around lines 256, 356).

- [ ] **Step 1: Check existing fleet.py test fixtures for a `.execute()` fake needing the same `num_retries` treatment as Task 1's `_FakeList`**

Run: `grep -n "def execute" tests/test_fleet.py tests/test_fleet_storage_drive.py tests/test_org_fleet.py`

If any fake's `execute` method doesn't accept `num_retries`, apply the identical fix from Task 1 Step 1 (add `num_retries=0` param, optionally record it) to that fake before proceeding — do not skip this, or Step 4 below will break existing fleet tests the same way it would have broken backup's.

- [ ] **Step 2: Write the failing test**

Add to `tests/test_fleet.py` (adapt the exact fake class name found in Step 1 — call it `FakeFilesFleet` below if none of the existing ones already record retries):

```python
def test_list_all_passes_num_retries(monkeypatch):
    from mcpbrain import fleet

    calls = []

    class _FakeExec:
        def execute(self, num_retries=0):
            calls.append(num_retries)
            return {"files": []}

    class _FakeFilesResource:
        def list(self, **kw):
            return _FakeExec()

    class _FakeDrive:
        def files(self):
            return _FakeFilesResource()

    fleet._list_all(_FakeDrive(), q="x")

    assert calls == [fleet._NUM_RETRIES]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_fleet.py -k num_retries -v`
Expected: `FAIL` — `AttributeError: module 'mcpbrain.fleet' has no attribute '_NUM_RETRIES'`.

- [ ] **Step 4: Add the constant and apply it**

In `mcpbrain/fleet.py`, near the top (after imports), add:

```python
# See mcpbrain.backup._NUM_RETRIES for the full rationale: googleapiclient's
# own num_retries already retries this error class with backoff, so every
# non-resumable Drive call here gets it. _upload_text's calls use
# MediaInMemoryUpload (a fixed in-memory buffer, not a resumable stream), so
# they have no "can't re-seek" problem either -- everything in this module
# is safe to retry.
_NUM_RETRIES = 5
```

Apply `num_retries=_NUM_RETRIES` to `_list_all`'s `.execute()` (around line 134), and to the four remaining bare calls: `_upload_text`'s `update()` and `create()` (around lines 167, 172), and the `get_media`/`delete` calls (around lines 256, 356).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_fleet.py tests/test_fleet_storage_drive.py tests/test_org_fleet.py -q`
Expected: `PASS`.

- [ ] **Step 6: Ruff and commit**

```bash
uv run ruff check mcpbrain/fleet.py tests/test_fleet.py
git add mcpbrain/fleet.py tests/test_fleet.py
git commit -m "fix(fleet): retry Drive calls with googleapiclient's num_retries"
```

---

### Task 3: `dashboard.py` + `auth.py` — same treatment (both single-call-site)

**Files:**
- Modify: `mcpbrain/dashboard.py` (1 call site), `mcpbrain/auth.py` (1 call site)
- Test: `tests/test_dashboard.py` (has an existing `TestCalendarTodayWithEvents` class with a `_make_service` `mock.MagicMock` helper — reuse it, don't hand-roll a fake), `tests/test_auth.py` (already exists)

**Interfaces:**
- Produces: `mcpbrain.dashboard._NUM_RETRIES = 5`, `mcpbrain.auth._NUM_RETRIES = 5`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_dashboard.py`'s existing `TestCalendarTodayWithEvents` class (it already has `_make_service`, which builds a `mock.MagicMock` with `service.events.return_value.list.return_value.execute.return_value` preset — reuse it exactly, then assert on the mock's call args instead of hand-rolling a fake):

```python
    def test_events_list_execute_passes_num_retries(self, tmp_path):
        svc = self._make_service([])

        with mock.patch("mcpbrain.auth.build_google_services",
                        return_value={"calendar_service": svc}):
            dashboard.calendar_today(str(tmp_path))

        svc.events.return_value.list.return_value.execute.assert_called_with(
            num_retries=dashboard._NUM_RETRIES)
```

Add to `tests/test_auth.py`:

```python
def test_fetch_google_name_passes_num_retries(monkeypatch):
    from unittest import mock
    from mcpbrain import auth

    fake_service = mock.MagicMock()
    fake_service.userinfo.return_value.get.return_value.execute.return_value = {
        "name": "Sam"
    }
    monkeypatch.setattr(auth, "build_service", lambda *a, **k: fake_service)

    name = auth.fetch_google_name(creds=object())

    assert name == "Sam"
    fake_service.userinfo.return_value.get.return_value.execute.assert_called_with(
        num_retries=auth._NUM_RETRIES)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_dashboard.py tests/test_auth.py -k num_retries -v`
Expected: `FAIL` — `AttributeError` on the missing `_NUM_RETRIES` constants.

- [ ] **Step 3: Add the constants and apply them**

In `mcpbrain/dashboard.py`, near the top:

```python
_NUM_RETRIES = 5  # see mcpbrain.backup._NUM_RETRIES for the full rationale
```

Change the `events().list(...).execute()` call (around line 221) to `.execute(num_retries=_NUM_RETRIES)`.

In `mcpbrain/auth.py`, near the top:

```python
_NUM_RETRIES = 5  # see mcpbrain.backup._NUM_RETRIES for the full rationale
```

Change `fetch_google_name` (around line 242):

```python
        info = build_service("oauth2", "v2", creds).userinfo().get() \
            .execute(num_retries=_NUM_RETRIES)
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_dashboard.py tests/test_auth.py -q`
Expected: `PASS`.

- [ ] **Step 5: Ruff and commit**

```bash
uv run ruff check mcpbrain/dashboard.py mcpbrain/auth.py tests/test_dashboard.py tests/test_auth.py
git add mcpbrain/dashboard.py mcpbrain/auth.py tests/test_dashboard.py tests/test_auth.py
git commit -m "fix(dashboard,auth): retry Drive/Calendar calls with num_retries"
```

---

### Task 4: `sync/drive.py` main sync path — same treatment (13 call sites)

**Files:**
- Modify: `mcpbrain/sync/drive.py`
- Test: `tests/test_drive_extraction.py` (new test — `_fetch_text` has no existing dedicated test in this file today, add one). `sync_drive`/`backfill_drive`/`reingest_files` are covered by `tests/test_drive_sync.py`, `tests/test_drive_changes.py`, `tests/test_drive_shared.py`, `tests/test_backfill_exec.py`, `tests/test_index_bounded.py` — all five must still pass after Step 4's mechanical edit, since none of their existing fakes assert on `num_retries` today (confirmed: none of them will break from the new kwarg being passed, since Python fakes with `def execute(self)` and no `**kwargs` would break, so check each of those five files' `execute` fakes accept arbitrary kwargs or `num_retries=0` before Step 4, per Step 1 below).

**Interfaces:**
- Produces: `mcpbrain.sync.drive._NUM_RETRIES = 5`.

**Context:** All 13 sites are `.get_media()` (a download — safe: a retry just re-issues the same idempotent GET), `.list()` (paginated file listing), `.getStartPageToken()`, or `.get()` (fresh metadata) — none is a resumable *upload* like backup's, so none has the re-seek problem. `reingest_files` (used by Phase 2) already builds its own per-thread `service_factory` to avoid the httplib2 thread-safety issue documented at that function's docstring — this task doesn't touch that mechanism, only adds the retry param to the plain calls.

- [ ] **Step 1: Confirm every drive.py test fixture's `execute` fake accepts `num_retries`**

Run: `grep -n "def execute" tests/test_drive_sync.py tests/test_drive_changes.py tests/test_drive_shared.py tests/test_backfill_exec.py tests/test_index_bounded.py`

For each `def execute(self):` found with no `num_retries` parameter, apply the Task 1 Step 1-style fixture fix (add `num_retries=0`, matching that file's existing fake-class style) before Step 4 below — otherwise Step 4's mechanical edit breaks every one of those files' existing tests with a `TypeError`.

- [ ] **Step 2: Write the failing test**

`_fetch_text(service, file_meta)` (`mcpbrain/sync/drive.py:134`) is the smallest function reaching one of the 13 bare `.execute()` calls — its `text/plain`-mimetype branch (around line 162) calls `service.files().get_media(...).execute()` directly, so it's callable in isolation without driving the whole `sync_drive`/`backfill_drive` pipeline:

```python
def test_fetch_text_get_media_passes_num_retries():
    from mcpbrain.sync import drive

    calls = []

    class _FakeExec:
        def execute(self, num_retries=0):
            calls.append(num_retries)
            return b"file contents"  # get_media returns raw bytes, decoded below

    class _FakeFiles:
        def get_media(self, **kw):
            return _FakeExec()

    class _FakeService:
        def files(self):
            return _FakeFiles()

    result = drive._fetch_text(_FakeService(), {"id": "f1", "mimeType": "text/plain"})

    assert result == "file contents"  # _fetch_text decodes bytes -> str
    assert calls == [drive._NUM_RETRIES]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_drive_extraction.py -k num_retries -v`
Expected: `FAIL` — `AttributeError: module 'mcpbrain.sync.drive' has no attribute '_NUM_RETRIES'`.

- [ ] **Step 4: Add the constant and apply it to all 13 sites**

In `mcpbrain/sync/drive.py`, near the top:

```python
_NUM_RETRIES = 5  # see mcpbrain.backup._NUM_RETRIES for the full rationale
```

Add `num_retries=_NUM_RETRIES` to every bare `.execute()` at lines 158, 162, 166, 261, 373, 445, 681, 719, 865, 908, 1155, 1213, 1267 (re-check exact line numbers against current `HEAD` before editing — other tasks in this plan don't touch this file, so they should be stable, but confirm with `grep -n "\.execute()" mcpbrain/sync/drive.py` first). Two of these return a dict key directly off the chained call (e.g. `.execute()["startPageToken"]`) — the pattern there is `.execute(num_retries=_NUM_RETRIES)["startPageToken"]`, same shape, just don't drop the trailing subscript.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_drive_extraction.py tests/test_drive_sync.py tests/test_drive_changes.py tests/test_drive_shared.py tests/test_backfill_exec.py tests/test_index_bounded.py -q`
Expected: `PASS`.

- [ ] **Step 6: Ruff and commit**

```bash
uv run ruff check mcpbrain/sync/drive.py tests/test_drive_extraction.py
git add mcpbrain/sync/drive.py tests/test_drive_extraction.py tests/test_drive_sync.py tests/test_drive_changes.py tests/test_drive_shared.py tests/test_backfill_exec.py tests/test_index_bounded.py
git commit -m "fix(sync/drive): retry every Drive call with num_retries"
```

---

### Task 5: `sync/calendar.py` — same treatment (2 call sites)

**Files:**
- Modify: `mcpbrain/sync/calendar.py`
- Test: `tests/test_calendar_sync.py` (already covers `_list_events` indirectly — confirmed via `grep -n "_list_events" tests/test_calendar_sync.py`)

**Interfaces:**
- Produces: `mcpbrain.sync.calendar._NUM_RETRIES = 5`.

- [ ] **Step 1: Confirm existing calendar.py test fixtures accept `num_retries`**

Run: `grep -n "def execute" tests/test_calendar_sync.py tests/test_calendar.py tests/test_calendar_graph.py`. Apply the Task 1 Step 1-style fix to any fake missing it.

- [ ] **Step 2: Write the failing test**

```python
def test_list_events_calls_pass_num_retries():
    from mcpbrain.sync import calendar

    calls = []

    class _FakeExec:
        def execute(self, num_retries=0):
            calls.append(num_retries)
            return {"items": [], "nextSyncToken": None}

    class _FakeEvents:
        def list(self, **kw):
            return _FakeExec()

    class _FakeService:
        def events(self):
            return _FakeEvents()

    calendar._list_events(_FakeService(), "primary", None,
                          "2026-01-01T00:00:00Z", "2026-12-31T00:00:00Z")

    assert calls == [calendar._NUM_RETRIES]
```

- [ ] **Step 3: Run test to verify it fails**

Run: `uv run pytest tests/test_calendar_sync.py -k num_retries -v`
Expected: `FAIL` — `AttributeError: module 'mcpbrain.sync.calendar' has no attribute '_NUM_RETRIES'`.

- [ ] **Step 4: Add the constant and apply it**

In `mcpbrain/sync/calendar.py`, near the top:

```python
_NUM_RETRIES = 5  # see mcpbrain.backup._NUM_RETRIES for the full rationale
```

Add `num_retries=_NUM_RETRIES` to both `.execute()` calls (lines 272 and 313).

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_calendar_sync.py tests/test_calendar.py tests/test_calendar_graph.py -q`
Expected: `PASS`.

- [ ] **Step 6: Ruff and commit**

```bash
uv run ruff check mcpbrain/sync/calendar.py tests/test_calendar_sync.py
git add mcpbrain/sync/calendar.py tests/test_calendar_sync.py
git commit -m "fix(sync/calendar): retry events().list() with num_retries"
```

---

### Task 6: `daemon.py` — rebuild `drive_service` after 2 consecutive backup failures

**Files:**
- Modify: `mcpbrain/daemon.py` (`maybe_backup`, `write_backup_state`)
- Test: `tests/test_daemon.py` (has `_RaisingFiles`, `_backup_config`, `_Clock`, `_backup_state` fixtures already built for exactly this failure path — reuse them, at lines ~795-835 and the two existing `test_maybe_backup_records_*` tests around line 1133)

**Interfaces:**
- Consumes: `mcpbrain.daemon._build_drive_service` (existing), `self._backup.drive_service` (existing `BackupConfig` field, already reassignable).
- Produces: `write_backup_state(home, *, ok, error=None) -> dict` (currently returns `None` — change it to return the `state` dict it already builds internally, at `mcpbrain/daemon.py:4013-4018`, so `maybe_backup` can read `consecutive_failures` off the return value directly instead of re-reading the file through another module's private helper).

**Context:** This layer is a second, smaller safety net beyond Task 1-5's per-call retry — for the case where the client/session itself is persistently broken (e.g. a stale token) in a way a single call's retry can't fix.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_daemon.py`, reusing the existing `_RaisingFiles`/`_backup_config`/`_Clock` fixtures exactly as `test_maybe_backup_records_a_failed_upload` does:

```python
def test_maybe_backup_rebuilds_drive_service_after_two_consecutive_failures(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _store_with_chunk(tmp_path)
    cfg = _backup_config(tmp_path, _RaisingFiles(list_response={"files": []}))
    daemon = Daemon(store, FakeEmbedder(), services={},
                    lock=SingleWriterLock(tmp_path / "d.lock"),
                    backup=cfg, backup_interval_s=0.0, clock=_Clock())

    rebuilt = {"count": 0}

    def _fake_build():
        rebuilt["count"] += 1
        return object()

    monkeypatch.setattr("mcpbrain.daemon._build_drive_service", _fake_build)

    daemon.maybe_backup()  # 1st failure: consecutive_failures becomes 1, no rebuild
    assert rebuilt["count"] == 0

    daemon.maybe_backup()  # 2nd failure: consecutive_failures becomes 2, rebuild fires
    assert rebuilt["count"] == 1
    assert daemon._backup.drive_service is not cfg.drive_service
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_daemon.py -k rebuilds_drive_service -v`
Expected: `FAIL` — `rebuilt["count"] == 0` (no rebuild logic exists yet).

- [ ] **Step 3: Make `write_backup_state` return its state dict, then implement the rebuild trigger**

In `mcpbrain/daemon.py`'s `write_backup_state` (line 3985-4022), change the last lines from writing-and-discarding to writing-and-returning:

```python
    try:
        path.write_text(json.dumps(state))
    except OSError as exc:
        log.warning("backup state write failed (continuing): %s", exc)
    return state
```

And update its signature/docstring return type: `def write_backup_state(home, *, ok: bool, error: str | None = None) -> dict:`.

In `maybe_backup`'s `except Exception as exc:` branch (around line 2168-2171):

```python
        except Exception as exc:  # noqa: BLE001 — backup must never crash the loop
            log.warning("periodic backup failed: %s", exc, exc_info=True)
            bstate = write_backup_state(home, ok=False, error=str(exc))
            failures = bstate.get("consecutive_failures") or 0
            if failures >= 2:
                log.warning(
                    "periodic backup: %d consecutive failures, rebuilding "
                    "drive_service", failures)
                try:
                    fresh = _build_drive_service()
                    with self._config_lock:
                        if self._backup is not None:
                            self._backup.drive_service = fresh
                except Exception as rebuild_exc:  # noqa: BLE001 — best-effort
                    log.warning("periodic backup: drive_service rebuild "
                               "failed: %s", rebuild_exc)
            return {"backed_up": False, "error": str(exc)}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_daemon.py -k rebuilds_drive_service -v`
Expected: `PASS`.

- [ ] **Step 5: Run the full daemon backup test suite**

Run: `uv run pytest tests/test_daemon.py -q`
Expected: `PASS` — including the two pre-existing `test_maybe_backup_records_*` tests, which must still pass unchanged (they don't inspect the return value of `write_backup_state`, only `backup_state.json`'s contents via the existing `_backup_state(tmp_path)` helper, so adding a return value is additive and doesn't break them).

- [ ] **Step 6: Ruff and commit**

```bash
uv run ruff check mcpbrain/daemon.py tests/test_daemon.py
git add mcpbrain/daemon.py tests/test_daemon.py
git commit -m "feat(daemon): rebuild drive_service after 2 consecutive backup failures

Closes the 'only a manual restart fixes it' gap for whatever the per-call
retry (added in Tasks 1-5) doesn't paper over -- a persistently broken
client/session, not a single transient blip."
```

---

## Phase 2 — Generalize the re-chunk repair sweep (spec §4)

**Must land before Phase 3's `CHUNKER_VERSION` bump (Task 13)** — bumping the version with no generalized sweep in place marks every non-Drive chunk stale with nothing able to act on it yet. Harmless, but land this phase first anyway.

### Task 7: `store.py` — generalize `stale_chunker_file_ids` to `stale_chunker_ids`

**Files:**
- Modify: `mcpbrain/store.py` (rename+generalize the method)
- Modify: `bin/repair.py` (both call sites: `phase_status` and `phase_reingest_stale`)
- Test: `tests/test_store_schema_p3.py` (new `stale_chunker_ids` test), plus `tests/test_drive_sync.py`, `tests/test_chunk_metadata.py`, `tests/test_doctor.py` (all three call the OLD method directly today and need their assertions updated — see Step 5)

**Interfaces:**
- Produces: `Store.stale_chunker_ids(version: int, limit: int) -> list[dict]`, each dict `{"source_type": "gdrive"|"gmail"|"calendar", "id": <file_id|thread_id|event_id>}`, ordered oldest-`MIN(rowid)`-first **within each source type**, Drive rows before Gmail rows before Calendar rows (sequential by source type, not interleaved — see spec §4's reasoning: the tool is re-run repeatedly and safely, so successive runs make cross-source progress without needing round-robin scheduling).

**Context:** The current method's docstring says "Drive-only. Gmail is 2% of the corpus... Gmail chunks pick up the new version as they naturally re-sync." That assumption doesn't hold: a Gmail message is immutable once received, and ordinary sync only ever touches NEW/changed messages (via the Gmail history API) — an already-ingested message with a stale `chunker_version` is never revisited by anything, which is exactly why the ~600 legacy Gmail/calendar chunks from the original investigation still exist. Correct the docstring's reasoning as part of this change, don't just silently drop it.

- [ ] **Step 1: Write the failing test**

```python
def test_stale_chunker_ids_covers_gdrive_gmail_and_calendar(tmp_path):
    from mcpbrain.store import Store

    s = Store(tmp_path / "test.db", dim=4)
    s.init()
    s.upsert_chunk("gdrive-f1-0", "old drive content", "h1",
                   {"source_type": "gdrive", "file_id": "f1", "chunker_version": 1})
    s.upsert_chunk("gmail-m1-body-0", "old gmail content", "h2",
                   {"source_type": "gmail", "thread_id": "t1", "chunker_version": 1})
    s.upsert_chunk("cal-e1-0", "old calendar content", "h3",
                   {"source_type": "calendar", "event_id": "e1", "chunker_version": 1})
    # A chunk already at the current version must NOT be selected.
    s.upsert_chunk("gdrive-f2-0", "current", "h4",
                   {"source_type": "gdrive", "file_id": "f2", "chunker_version": 2})

    out = s.stale_chunker_ids(version=2, limit=100)

    assert {"source_type": "gdrive", "id": "f1"} in out
    assert {"source_type": "gmail", "id": "t1"} in out
    assert {"source_type": "calendar", "id": "e1"} in out
    assert not any(item["id"] == "f2" for item in out)
    # Drive entries sort before Gmail before Calendar.
    types_in_order = [item["source_type"] for item in out]
    assert types_in_order == sorted(
        types_in_order, key=lambda t: {"gdrive": 0, "gmail": 1, "calendar": 2}[t])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_schema_p3.py -k stale_chunker_ids -v`
Expected: `FAIL` — `AttributeError: 'Store' object has no attribute 'stale_chunker_ids'`.

- [ ] **Step 3: Implement `stale_chunker_ids`, replacing `stale_chunker_file_ids`**

In `mcpbrain/store.py`, replace the existing `stale_chunker_file_ids` (around line 1700) with:

```python
    def stale_chunker_ids(self, version: int, limit: int) -> list[dict]:
        """File/thread/event ids with at least one chunk written by an older
        chunker, across every source type.

        The level-triggered selector for bin/repair.py's re-ingest phase: no
        queue, no cursor, no new state. Re-running walks forward because a
        repaired item stops matching, and an interrupted run simply resumes.

        Distinct ids (not doc_ids) because re-ingest operates per FILE/
        THREAD/EVENT: one fetch replaces all of that owner's chunks at once.

        Originally Drive-only, on the theory that "Gmail chunks pick up the
        new version as they naturally re-sync." That assumption doesn't
        hold: a Gmail message is immutable once received, and ordinary sync
        only ever touches NEW/changed messages via the history API -- an
        already-ingested message's chunker_version never gets revisited by
        anything else, so it stays stale forever without this. Generalized
        to cover gmail/calendar too.

        Ordered by MIN(rowid) within each source type, Drive rows first then
        Gmail then Calendar (not interleaved) -- see bin/repair.py's
        phase_reingest_stale for why sequential-by-source is enough.
        """
        out: list[dict] = []
        with self._connect() as db:
            for source_type, id_field in (
                ("gdrive", "file_id"), ("gmail", "thread_id"),
                ("calendar", "event_id"),
            ):
                rows = db.execute(
                    f"SELECT json_extract(metadata,'$.{id_field}') AS oid, "
                    f"MIN(rowid) AS r FROM chunks "
                    f"WHERE json_extract(metadata,'$.source_type')=? "
                    f"  AND json_extract(metadata,'$.{id_field}') IS NOT NULL "
                    f"  AND COALESCE(json_extract(metadata,'$.chunker_version'),0) < ? "
                    f"GROUP BY oid ORDER BY r LIMIT ?",
                    (source_type, version, limit - len(out)),
                ).fetchall()
                out.extend({"source_type": source_type, "id": r["oid"]} for r in rows)
                if len(out) >= limit:
                    break
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_store_schema_p3.py -k stale_chunker_ids -v`
Expected: `PASS`.

- [ ] **Step 5: Update every existing test that calls the old `stale_chunker_file_ids` directly**

The old method is called and asserted on by name in THREE other test files, expecting the old `list[str]` return shape — renaming without updating these breaks all of them silently unless caught by the full-suite run. Fix each explicitly, now:

In `tests/test_drive_sync.py`, six assertions (lines ~1221, 1244, 1292, 1509, 1528, 1617) follow the pattern `store.stale_chunker_file_ids(CHUNKER_VERSION, limit=10) == [...]` or `== []`. Replace every one of them: rename the method call to `stale_chunker_ids` and unwrap the new `[{"source_type": ..., "id": ...}]` shape to the same flat list of ids the old assertion expected, e.g.:

```python
    assert [d["id"] for d in store.stale_chunker_ids(CHUNKER_VERSION, limit=10)] == ["empty1"]
```

(and the `== []` cases become `assert store.stale_chunker_ids(CHUNKER_VERSION, limit=10) == []`, since an empty list unwraps to an empty list either way).

In `tests/test_chunk_metadata.py`, `test_stale_chunker_file_ids_selects_only_out_of_date_drive_files` (line 141) becomes:

```python
def test_stale_chunker_ids_selects_only_out_of_date_drive_files(tmp_path):
    """The level-triggered selector. No queue, no cursor: re-running walks
    forward because each repaired file stops matching. Same shape as
    reflow_outdated_chunks, which is the established pattern here."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-old-0", "legacy text", "h1",
                       {"source_type": "gdrive", "file_id": "old"})          # no version
    store.upsert_chunk("gdrive-mid-0", "half text", "h2",
                       {"source_type": "gdrive", "file_id": "mid",
                        "chunker_version": 1})
    store.upsert_chunk("gdrive-new-0", "fresh text", "h3",
                       {"source_type": "gdrive", "file_id": "new",
                        "chunker_version": 2})

    got = [d["id"] for d in store.stale_chunker_ids(2, limit=10)]

    assert sorted(got) == ["mid", "old"]
```

And `test_stale_chunker_file_ids_respects_its_limit_and_is_gmail_free` (line 161) — this test's own PREMISE is now wrong (its docstring cites the exact "decision 4" reasoning this task just corrected). Replace it, don't just reshape its assertion:

```python
def test_stale_chunker_ids_respects_its_limit_across_source_types(tmp_path):
    """Gmail is no longer excluded (that assumption didn't hold -- see
    stale_chunker_ids' docstring) -- this now tests that `limit` is respected
    as a TOTAL across source types, not that Gmail is filtered out."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    for i in range(5):
        store.upsert_chunk(f"gdrive-f{i}-0", f"text {i}", f"h{i}",
                           {"source_type": "gdrive", "file_id": f"f{i}"})
    store.upsert_chunk("gmail-m1-body-0", "mail text", "hm",
                       {"source_type": "gmail", "thread_id": "t1"})

    got = store.stale_chunker_ids(2, limit=3)

    assert len(got) == 3
    # The limit is hit within the gdrive batch (5 candidates, limit 3) before
    # gmail's single candidate is even considered -- sequential by source type.
    assert all(d["source_type"] == "gdrive" for d in got)
```

In `tests/test_doctor.py`, line 406 becomes:

```python
    assert [d["id"] for d in store.stale_chunker_ids(2, limit=10)] == ["f1"]
```

- [ ] **Step 6: Update `bin/repair.py`'s two call sites**

`phase_status` (around line 82):

```python
    stale = len(store.stale_chunker_ids(CHUNKER_VERSION, limit=100_000))
```

Leave `phase_reingest_stale`'s call site for Task 9 (it needs the dispatch logic, not just the renamed call).

- [ ] **Step 7: Run the full store + repair + affected test suites**

Run: `uv run pytest tests/test_store_schema_p3.py tests/test_drive_sync.py tests/test_chunk_metadata.py tests/test_doctor.py -q`
Expected: `PASS`.
Run: `uv run pytest tests/test_repair.py -q`
Expected: `test_repair.py` will still have a broken `phase_reingest_stale` call until Task 9 — confirm the ONLY failures are inside `phase_reingest_stale`-related tests, and that nothing else regressed. That's expected at this checkpoint.

- [ ] **Step 8: Ruff and commit**

```bash
uv run ruff check mcpbrain/store.py bin/repair.py tests/test_store_schema_p3.py tests/test_drive_sync.py tests/test_chunk_metadata.py tests/test_doctor.py
git add mcpbrain/store.py bin/repair.py tests/test_store_schema_p3.py tests/test_drive_sync.py tests/test_chunk_metadata.py tests/test_doctor.py
git commit -m "feat(store): generalize stale_chunker_file_ids to cover gmail/calendar

The 'Gmail picks up the new version as it naturally re-syncs' assumption
in the old docstring doesn't hold -- an already-ingested message is
immutable and never revisited by ordinary sync, so it stays on the old
chunker version forever without this."
```

---

### Task 8: `sync/gmail.py` — new `reingest_messages`, mirroring `reingest_files`

**Files:**
- Modify: `mcpbrain/sync/gmail.py` (new function)
- Test: `tests/test_gmail_sync.py` (already covers `backfill_gmail` — e.g. `test_backfill_gmail_can_narrow_the_query` — add `reingest_messages` tests alongside it)

**Interfaces:**
- Consumes: `sync/gmail.py::_fetch_one(service, mid, *, fetch_attachments, att_report)` (existing), `mcpbrain.chunking.chunk_text`/`CHUNKER_VERSION` (existing).
- Produces: `reingest_messages(service, store, thread_ids: list[str], *, report: dict | None = None) -> dict` returning `{"messages": n_reingested, "missing": n, "failed": n}`.

**Context:** Unlike Drive, an email's content never shrinks after the fact — no B5-style orphan sweep needed, this is purely "re-chunk under the current chunker version." But it MUST replicate `reingest_files`'s convergence guard: a 404'd/inaccessible message gets its existing chunks stamped with the current `chunker_version` anyway, or the selector re-fetches the same dead thread on every single run forever (the exact failure Drive's `reingest_files` measured live before this guard existed: ~46 repeat fetches of the same 10 files in 41 minutes).

- [ ] **Step 1: Write the failing test**

```python
def test_reingest_messages_rechunks_a_stale_thread(tmp_path):
    from mcpbrain.store import Store
    from mcpbrain.sync.gmail import reingest_messages

    store = Store(tmp_path / "test.db", dim=4)
    store.init()
    store.upsert_chunk("gmail-m1-body-0", "old short content", "h1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    class _FakeMessages:
        def get(self, *, userId, id, format):
            class _Exec:
                def execute(self, num_retries=0):
                    return {
                        "id": "m1", "threadId": "t1", "labelIds": ["INBOX"],
                        "payload": {
                            "mimeType": "text/plain",
                            "headers": [
                                {"name": "From", "value": "a@b.com"},
                                {"name": "Date", "value": "Mon, 1 Jun 2026 00:00:00 +0000"},
                                {"name": "Subject", "value": "Re: test"},
                            ],
                            "body": {"data": "bmV3IGNvbnRlbnQ="},  # "new content"
                        },
                    }
            return _Exec()

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    summary = reingest_messages(_FakeService(), store, ["t1"])

    assert summary["messages"] == 1
    row = store._connect().__enter__().execute(
        "SELECT metadata FROM chunks WHERE doc_id LIKE 'gmail-m1-%'").fetchone()
    import json
    assert json.loads(row["metadata"])["chunker_version"] == 2


def test_reingest_messages_stamps_version_on_a_missing_message(tmp_path):
    from mcpbrain.store import Store
    from mcpbrain.sync.gmail import reingest_messages
    from googleapiclient.errors import HttpError

    store = Store(tmp_path / "test.db", dim=4)
    store.init()
    store.upsert_chunk("gmail-m1-body-0", "old content", "h1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    class _Resp:
        status = 404

    class _FakeMessages:
        def get(self, *, userId, id, format):
            class _Exec:
                def execute(self, num_retries=0):
                    raise HttpError(_Resp(), b"not found")
            return _Exec()

    class _FakeUsers:
        def messages(self):
            return _FakeMessages()

    class _FakeService:
        def users(self):
            return _FakeUsers()

    summary = reingest_messages(_FakeService(), store, ["t1"])

    assert summary["missing"] == 1
    import json
    row = store._connect().__enter__().execute(
        "SELECT metadata FROM chunks WHERE doc_id='gmail-m1-body-0'").fetchone()
    # Convergence guard: stamped to the current version even though the
    # message was 404, so the selector stops re-fetching this dead thread.
    assert json.loads(row["metadata"])["chunker_version"] == 2
```

(Check `_fetch_one`'s exact return shape at `mcpbrain/sync/gmail.py:239` and the real chunking pipeline it feeds into — around `sync_gmail`'s body — before finalizing the base64 payload and header shape above; match whatever the real `sync_gmail` path already exercises in its own existing tests, don't invent a divergent fixture shape.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_gmail_sync.py -k reingest_messages -v`
Expected: `FAIL` — `ImportError: cannot import name 'reingest_messages'`.

- [ ] **Step 3: Implement `reingest_messages`**

In `mcpbrain/sync/gmail.py`, add near `backfill_gmail`. It reuses `normalise_gmail` (already imported at the top of this file: `from mcpbrain.sync.normalise import normalise_gmail`, line 17) — the exact same per-message normalise+chunk call `backfill_gmail`'s own `_write` closure uses, not a reimplementation. `Store` has no `doc_ids_for_thread`; use the existing `store.thread_chunks(thread_id)` (`mcpbrain/store.py:2967`, returns `[{"doc_id", "text", "metadata"}, ...]`) and pull `doc_id` off each result:

```python
def reingest_messages(service, store, thread_ids: list, *,
                      report: dict | None = None) -> dict:
    """Re-fetch and re-chunk specific Gmail threads by id, under the current
    chunker version.

    Unlike Drive, a message's content never shrinks after the fact --
    immutable once received -- so there's no orphan sweep here, just
    re-chunking. Isolation is per THREAD: a 404 (deleted/inaccessible) is
    NOT left alone the way it would be if nothing acted on it -- its
    existing chunks are stamped to the current chunker_version anyway, or
    the selector (store.stale_chunker_ids) re-fetches the same dead thread
    on every single run forever. This mirrors sync/drive.py's
    reingest_files, which hit exactly that non-convergence bug live (~46
    repeat fetches of the same 10 files in 41 minutes) before its own
    missing/empty stamping guard was added.

    Returns {"messages": n_reingested, "missing": n, "failed": n}.
    """
    summary = {"messages": 0, "missing": 0, "failed": 0}
    for thread_id in thread_ids:
        doc_ids = [c["doc_id"] for c in store.thread_chunks(thread_id)]
        message_ids = {
            d.split("-")[1] for d in doc_ids if d.startswith("gmail-")
        } or {thread_id}
        for mid in message_ids:
            try:
                raw, _att = _fetch_one(service, mid, fetch_attachments=False,
                                       att_report=report)
            except Exception as exc:  # noqa: BLE001 -- one bad message must not end the run
                log.warning("reingest_messages: %s failed: %s", mid, exc)
                summary["failed"] += 1
                continue
            if raw is None:
                # 404 -- stamp the existing chunks so the selector stops
                # re-picking this dead message, exactly like Drive's
                # missing/empty outcomes. Exact id-segment match, not a
                # substring check -- "m1" must not match a doc_id for "m10".
                for doc_id in [d for d in doc_ids if d.split("-")[1] == mid]:
                    store.patch_chunk_metadata(
                        doc_id, chunker_version=CHUNKER_VERSION,
                        reextract_missing=True)
                summary["missing"] += 1
                continue
            skips: dict = {}
            for c in normalise_gmail(raw, report=skips):
                store.upsert_chunk(c.doc_id, c.text, c.content_hash, c.metadata)
            summary["messages"] += 1
    return summary
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_gmail_sync.py -k reingest_messages -v`
Expected: `PASS`.

- [ ] **Step 5: Run the full gmail sync test suite**

Run: `uv run pytest tests/test_gmail_sync.py -q`
Expected: `PASS`.

- [ ] **Step 6: Ruff and commit**

```bash
uv run ruff check mcpbrain/sync/gmail.py tests/test_gmail_sync.py
git add mcpbrain/sync/gmail.py tests/test_gmail_sync.py
git commit -m "feat(sync/gmail): add reingest_messages, mirroring reingest_files

Replicates the same non-convergence guard Drive's reingest_files already
needed: a missing/inaccessible message gets its chunks stamped to the
current chunker_version anyway, so the selector doesn't re-fetch a dead
thread forever."
```

---

### Task 9: `bin/repair.py` — generalize `phase_reingest_stale` to dispatch by source type

**Files:**
- Modify: `bin/repair.py`
- Test: `tests/test_repair.py`

**Interfaces:**
- Consumes: `store.stale_chunker_ids` (Task 7), `sync/gmail.py::reingest_messages` (Task 8), `sync/drive.py::reingest_files` (existing, unchanged), `sync/calendar.py::backfill_calendar_window` (existing).

**Context:** Calendar's stale-chunk volume is tiny (4, in the original investigation) — reuse `backfill_calendar_window` scoped to a narrow window around the stale event's own start time rather than building a bespoke single-event-refetch primitive.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_repair.py` (matching the existing `test_reingest_phase_reports_the_stale_file_count` style at line 95):

```python
def test_reingest_stale_dispatches_gmail_threads_through_reingest_messages(tmp_path, monkeypatch):
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gmail-m1-body-0", "old", "h1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    called = {}
    def _fake_reingest_messages(service, store, thread_ids, **kw):
        called["thread_ids"] = thread_ids
        return {"messages": 1, "missing": 0, "failed": 0}
    monkeypatch.setattr("mcpbrain.sync.gmail.reingest_messages",
                        _fake_reingest_messages)

    out = _run("reingest-stale", "--apply", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert called.get("thread_ids") == ["t1"]
```

(Check `_run`'s exact helper signature at the top of `tests/test_repair.py` before finalizing — it's already used by every other phase test in this file.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_repair.py -k dispatches_gmail -v`
Expected: `FAIL` — the monkeypatched `reingest_messages` never gets called (current `phase_reingest_stale` only knows about Drive).

- [ ] **Step 3: Implement the dispatch**

Replace `phase_reingest_stale` (currently `bin/repair.py:130`) with:

```python
def phase_reingest_stale(store, apply: bool, *, limit: int, workers: int = 1) -> int:
    items = store.stale_chunker_ids(CHUNKER_VERSION, limit=limit)
    by_type: dict = {}
    for item in items:
        by_type.setdefault(item["source_type"], []).append(item["id"])
    print(f"[reingest-stale] {len(items)} item(s) selected (limit {limit}): "
          f"{ {k: len(v) for k, v in by_type.items()} }")
    if not apply:
        print("[reingest-stale] dry run — nothing fetched; pass --apply to write")
        return 0

    from mcpbrain.auth import build_google_services
    services = build_google_services()

    if by_type.get("gdrive"):
        from mcpbrain.sync.drive import flush_skip_report, reingest_files
        drive = services.get("drive_service")
        if drive is None:
            print("[reingest-stale] no drive_service (token lacks the Drive "
                  "scope); re-authenticate with `mcpbrain setup`", file=sys.stderr)
        else:
            service_factory = (
                (lambda: build_google_services().get("drive_service"))
                if workers > 1 else None)
            report: dict = {}
            summary = reingest_files(drive, store, by_type["gdrive"],
                                     max_workers=workers,
                                     service_factory=service_factory, report=report)
            flush_skip_report(store, report, source="repair:reingest")
            print(f"[reingest-stale] drive: {summary}")

    if by_type.get("gmail"):
        from mcpbrain.sync.gmail import reingest_messages
        gmail = services.get("gmail_service")
        if gmail is None:
            print("[reingest-stale] no gmail_service (token lacks the Gmail "
                  "scope); re-authenticate with `mcpbrain setup`", file=sys.stderr)
        else:
            summary = reingest_messages(gmail, store, by_type["gmail"])
            print(f"[reingest-stale] gmail: {summary}")

    if by_type.get("calendar"):
        from mcpbrain.sync.calendar import backfill_calendar_window
        calendar_svc = services.get("calendar_service")
        if calendar_svc is None:
            print("[reingest-stale] no calendar_service (token lacks the "
                  "Calendar scope); re-authenticate with `mcpbrain setup`",
                  file=sys.stderr)
        else:
            # Volume is tiny (4 in the original investigation) -- not worth
            # a bespoke single-event-refetch primitive. Re-run the existing
            # window backfill over a wide-enough span to catch them; a
            # narrow per-event window would need each event's own start
            # time, which isn't cheaply available from just the event id
            # here, so this reuses the existing all-events-in-window path.
            from datetime import datetime, timedelta, timezone
            now = datetime.now(timezone.utc)
            time_min = (now - timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ")
            time_max = (now + timedelta(days=730)).strftime("%Y-%m-%dT%H:%M:%SZ")
            n = backfill_calendar_window(calendar_svc, store, time_min=time_min,
                                         time_max=time_max)
            print(f"[reingest-stale] calendar: refreshed window, {n} events")

    return 0
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_repair.py -k dispatches_gmail -v`
Expected: `PASS`.

- [ ] **Step 5: Run the full repair test suite**

Run: `uv run pytest tests/test_repair.py -q`
Expected: `PASS` — including the pre-existing Drive-only tests (`test_reingest_stale_wires_workers_into_reingest_files`, `test_reingest_stale_aggregates_skips_instead_of_writing_per_file`, `test_reingest_stale_still_defaults_to_500`), which must still pass unchanged since `by_type["gdrive"]` dispatch is byte-for-byte the same call as before.

- [ ] **Step 6: Ruff and commit**

```bash
uv run ruff check bin/repair.py tests/test_repair.py
git add bin/repair.py tests/test_repair.py
git commit -m "feat(repair): dispatch reingest-stale across gdrive/gmail/calendar

Drive's own reingest_files call is unchanged; gmail and calendar get the
same treatment via the newly generalized store.stale_chunker_ids selector."
```

---

## Phase 3 — Tabular rendering (spec §2)

### Task 10: `normalise_rows` — minimum multi-row support threshold, not max

**Files:**
- Modify: `mcpbrain/sync/tabular.py` (`normalise_rows`)
- Test: `tests/test_tabular.py`

**Interfaces:**
- Produces: `normalise_rows` keeps its existing signature (`rows: list[list[str]]) -> list[list[str]]`) — pure internal-algorithm change, no callers need to change.

**Context:** Current logic trims trailing columns using the MAX non-empty column index across every row — exactly what one anomalous row (a title banner with a stray non-empty cell far to the right) defeats. Fix: a column only counts as real if at least `max(2, len(kept) // 100)` distinct rows have non-empty content there. One outlier row can never clear a support threshold of 2 on its own; a column used by even a modest minority of rows still counts as real (avoiding a median's failure mode: silently dropping a legitimately-sparse-but-real column used by fewer than half the rows).

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_tabular.py`:

```python
def test_normalise_rows_ignores_a_single_outlier_wide_row():
    # 700 normal rows (4 real columns) plus one title-banner row with a
    # single stray non-empty cell at column 19999 -- the exact reproducing
    # case from the live investigation (a Fixed Assets Register spreadsheet).
    header = ["Item", "Cost", "Date", "Location"]
    normal_rows = [["Chair", "50", "2024-01-01", "Hall A"] for _ in range(700)]
    outlier = [""] * 19999 + ["FIXED ASSETS REGISTER as at 31 December 2022"]
    rows = [header] + normal_rows + [outlier]

    out = tabular.normalise_rows(rows)

    # The outlier row is trimmed to the real width, not the other way around.
    assert all(len(r) <= 4 for r in out), (
        f"expected every row trimmed to width<=4, got a row of length "
        f"{max(len(r) for r in out)}")


def test_normalise_rows_keeps_a_genuinely_sparse_real_column():
    # A "notes" column only 5 of 50 rows populate is still real -- it must
    # NOT be dropped just because it's a minority.
    header = ["Item", "Cost", "Notes"]
    rows_without_notes = [["Chair", "50", ""] for _ in range(45)]
    rows_with_notes = [["Desk", "200", "damaged"] for _ in range(5)]
    rows = [header] + rows_without_notes + rows_with_notes

    out = tabular.normalise_rows(rows)

    assert all(len(r) == 3 for r in out), (
        "the Notes column (5/50 rows, above the support floor of 2) must survive")
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tabular.py -k normalise_rows -v`
Expected: `FAIL` on the first test (current max-based logic keeps the outlier's width of 20000) — the second test may already pass by coincidence with current logic; confirm both are meaningful by checking actual output before and after.

- [ ] **Step 3: Implement the minimum-support fix**

Replace `normalise_rows` in `mcpbrain/sync/tabular.py`:

```python
def normalise_rows(rows: list[list[str]]) -> list[list[str]]:
    """Drop entirely-empty rows and trim trailing columns with no real support.

    Only TRAILING columns: an interior blank column can be meaningful (a
    spacer between a budget's actuals and its variance), and dropping it
    would misalign the header against its values.

    A column counts as real only if at least max(2, len(kept)//100) distinct
    rows have non-empty content there -- NOT simply "the max index any row
    reaches". A single anomalous row (a title banner, a stray far-right
    formatted-but-blank cell -- both routine in real Excel files, since
    Excel's "used range" is inflated by formatting alone) can never clear a
    support floor of 2 on its own, so it can no longer single-handedly
    dictate the whole table's width the way a bare max did. A genuinely
    sparse-but-real column (used by a legitimate minority of rows) still
    clears the floor and survives -- a median would have dropped it if used
    by fewer than half the rows, which is why this isn't a median.
    """
    kept = [r for r in rows if any((c or "").strip() for c in r)]
    if not kept:
        return []
    max_len = max(len(r) for r in kept)
    support = [0] * max_len
    for r in kept:
        for i, c in enumerate(r):
            if (c or "").strip():
                support[i] += 1
    floor = max(2, len(kept) // 100)
    width = 0
    for i in range(max_len - 1, -1, -1):
        if support[i] >= floor:
            width = i + 1
            break
    if width == 0:
        # No column clears the floor (e.g. a genuinely tiny 1-2 row table) --
        # fall back to the simple max so a small legitimate table isn't
        # wrongly trimmed to zero width.
        for r in kept:
            for i, c in enumerate(r):
                if (c or "").strip():
                    width = max(width, i + 1)
    return [r[:width] for r in kept]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tabular.py -k normalise_rows -v`
Expected: `PASS`.

- [ ] **Step 5: Run the full tabular test suite**

Run: `uv run pytest tests/test_tabular.py -q`
Expected: `PASS` — every existing test using `normalise_rows`-derived `Table` fixtures must still pass (most tests build `Table` objects directly, bypassing `normalise_rows`, so this should be low-risk; confirm by reading the failure list if anything breaks).

- [ ] **Step 6: Ruff and commit**

```bash
uv run ruff check mcpbrain/sync/tabular.py tests/test_tabular.py
git add mcpbrain/sync/tabular.py tests/test_tabular.py
git commit -m "fix(tabular): trim trailing columns by minimum row support, not max

A single anomalous row (a title banner with one stray far-right non-empty
cell -- routine in real Excel files) could dictate the whole table's
rendered width. Requiring support from >=2 rows defeats that while still
preserving a genuinely sparse-but-real column used by a legitimate minority."
```

---

### Task 11: `render_chunks` redesign — schema-enriched row sentences

**Files:**
- Modify: `mcpbrain/sync/tabular.py` (`render_chunks`; delete `_fit_row`, `_md_row`)
- Test: `tests/test_tabular.py`

**Interfaces:**
- Produces: `render_chunks(tables: list[Table], *, file_name: str, max_chars: int) -> list[tuple[str, dict]]` — signature unchanged; only the rendered `text` shape changes. Metadata keys (`table_role`, `row_start`, `row_end`, `rows_total`, `truncated`, `sheet`) unchanged.

**Context:** Each row now renders independently as `"{header[i]}: {value[i]}"` pairs for non-empty cells only — no shared `width`/`header_line`/`sep_line` across the sheet, so even without Task 10's fix, one row's phantom columns can never leak into another row's output (defense in depth). This is also the research-favored representation for embedding quality (schema-enriched row sentences outperform raw markdown-grid tables for retrieval).

- [ ] **Step 1: Write the failing tests**

The existing test `test_every_row_group_chunk_repeats_the_header` (in `tests/test_tabular.py`) asserts a markdown-grid header line (`"| Date | Account | Description | Amount |"`) — that assertion is now WRONG for the new design and must be rewritten, not left in place expecting old output. Replace it and add new coverage:

```python
def test_row_sentences_use_schema_enriched_format():
    chunks = tabular.render_chunks([_table()], file_name="Ledger.xlsx", max_chars=1800)
    rows = [(t, m) for t, m in chunks if m["table_role"] == "rows"]

    assert rows, "expected at least one row-group chunk"
    text, _meta = rows[0]
    assert "Date: 2024-03-01" in text
    assert "Account: 4521" in text
    assert "Description: Office supplies" in text
    assert "Amount: 42.00" in text
    # No markdown grid artifacts survive.
    assert "| Date | Account" not in text
    assert "---" not in text


def test_empty_cells_are_never_rendered():
    header = ["Item", "Cost", "Notes"]
    rows = [["Chair", "50", ""]]  # Notes is empty for this row
    table = tabular.Table(sheet="S", header=header, rows=rows, rows_total=1)

    chunks = tabular.render_chunks([table], file_name="f.xlsx", max_chars=1800)
    text = [t for t, m in chunks if m["table_role"] == "rows"][0][0]

    assert "Notes" not in text, "an empty cell must not render at all"


def test_phantom_wide_row_stays_under_chunk_chars_regardless():
    # Belt-and-suspenders: even without Task 10's normalise_rows fix, one
    # anomalous row with thousands of non-empty phantom cells must not blow
    # the chunk budget, because the field-count safety valve bounds it.
    header = ["Item"] + [f"col{i}" for i in range(5000)]
    phantom_row = ["Chairs"] + [f"v{i}" for i in range(5000)]  # all non-empty
    table = tabular.Table(sheet="S", header=header, rows=[phantom_row], rows_total=1)

    chunks = tabular.render_chunks([table], file_name="f.xlsx", max_chars=1800)
    for text, meta in chunks:
        if meta["table_role"] == "rows":
            assert len(text) <= 1800 + 200, (
                f"row-group chunk of {len(text)} chars exceeds budget even "
                "with the field-count safety valve")


def test_row_with_too_many_fields_gets_elided():
    header = ["Item"] + [f"col{i}" for i in range(60)]
    row = ["Chairs"] + [f"v{i}" for i in range(60)]
    table = tabular.Table(sheet="S", header=header, rows=[row], rows_total=1)

    chunks = tabular.render_chunks([table], file_name="f.xlsx", max_chars=100_000)
    text = [t for t, m in chunks if m["table_role"] == "rows"][0][0]

    assert "more fields" in text
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tabular.py -q`
Expected: `FAIL` on the new tests and on the now-incompatible `test_every_row_group_chunk_repeats_the_header` (current renderer still produces a markdown grid).

- [ ] **Step 3: Delete the old markdown-grid test and implement the redesign**

In `tests/test_tabular.py`, delete `test_every_row_group_chunk_repeats_the_header` and `test_each_row_group_names_its_sheet_and_row_range` if the latter asserts on markdown-specific formatting — read it first; if it only asserts on the `### Sheet: ...` title line (which survives unchanged in the new design), keep it as-is.

In `mcpbrain/sync/tabular.py`, delete `_md_row` and `_fit_row` entirely, and replace `render_chunks` and its row-loop:

```python
_MAX_FIELDS_PER_ROW = 40  # generous for a real spreadsheet, tight enough to
                          # bound a phantom-column sheet even without
                          # normalise_rows' own fix (defense in depth).


def _row_sentence(header: list[str], row: list[str]) -> str:
    """Render one row as 'Header: Value; Header: Value; ...' for non-empty
    cells only -- an empty cell is simply never rendered, which is what
    makes this immune to phantom trailing columns by construction. No shared
    width/header_line/sep_line across the sheet: one anomalous row can never
    inflate another row's output."""
    pairs = []
    for i, value in enumerate(row):
        v = (value or "").strip()
        if not v:
            continue
        h = header[i] if i < len(header) and header[i].strip() else f"col{i}"
        pairs.append(f"{h}: {_cell(v)}")
    if len(pairs) > _MAX_FIELDS_PER_ROW:
        extra = len(pairs) - _MAX_FIELDS_PER_ROW
        pairs = pairs[:_MAX_FIELDS_PER_ROW] + [f"(+{extra} more fields)"]
    return "; ".join(pairs)


def render_chunks(tables: list[Table], *, file_name: str,
                  max_chars: int) -> list[tuple[str, dict]]:
    """Render Tables to (chunk_text, metadata_extras) pairs.

    Each row renders as a schema-enriched sentence (see _row_sentence) --
    matches the RAG-chunking research finding that row-wise "schema-enriched"
    sentences outperform raw markdown-grid table embeddings for retrieval,
    and is immune to the phantom-column bug by construction (an empty cell
    is never rendered, so there's no shared width to compute or get wrong).
    Content-free chunks are never emitted.
    """
    out: list[tuple[str, dict]] = []
    for t in tables:
        base = {"sheet": t.sheet, "rows_total": t.rows_total,
                "rows_captured": len(t.rows), "truncated": t.truncated}
        out.append((_summary_text(file_name, t), {**base, "table_role": "summary"}))

        group: list[str] = []
        start = 1
        for n, row in enumerate(t.rows, start=1):
            line = _row_sentence(t.header, row)
            candidate = group + [line]
            if group and _rendered_size(_title(t, start, n), "", "",
                                        candidate) > max_chars:
                out.append(_emit(t, "", "", group, base, start, start + len(group) - 1))
                start, group = n, []
            group.append(line)
        if group:
            out.append(_emit(t, "", "", group, base, start, start + len(group) - 1))
    return [(text, meta) for text, meta in out if has_content(text)]
```

`_rendered_size`, `_title`, `_summary_text` stay unchanged in signature; `_emit` also stays unchanged in signature — it already takes `header_line`/`sep_line` as plain strings to join, so passing `""` for both means the joined text is just `title + "\n" + "\n".join(group)` (verify this against `_emit`'s actual body — read it before finalizing; if it hard-codes a specific join format assuming a non-empty header/sep, adjust `_emit` itself to skip empty strings when joining rather than passing `""` placeholders that leave stray blank lines).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tabular.py -q`
Expected: `PASS`.

- [ ] **Step 5: Ruff and commit**

```bash
uv run ruff check mcpbrain/sync/tabular.py tests/test_tabular.py
git add mcpbrain/sync/tabular.py tests/test_tabular.py
git commit -m "feat(tabular): render rows as schema-enriched sentences, not markdown grids

Matches the RAG-chunking research finding that row-wise 'Header: Value'
sentences outperform raw markdown-table embeddings, and is immune to the
phantom-column bug by construction -- an empty cell is never rendered, so
there's no shared width computation left to get wrong. Deletes _fit_row/
_md_row, which existed only to bound the old design's failure mode."
```

---

### Task 12: Skip dense embedding for table-subtype content, keep FTS

**Files:**
- Modify: `mcpbrain/store.py` (`write_embedding`)
- Modify: `mcpbrain/index.py` (`index_pending`)
- Modify: `mcpbrain/config.py` (new `embed_skip_tabular_enabled`)
- Test: `tests/test_store.py` (already has several `write_embedding` call sites, e.g. line 502), `tests/test_config.py` (houses `salience_gate_enabled`-style flag tests), `tests/test_index.py` (already has `test_index_pending_embeds_and_marks_done` etc.)

**Interfaces:**
- Produces: `Store.write_embedding(rowid: int, vector: list[float] | None, *, home=None) -> None` (now accepts `None`), `config.embed_skip_tabular_enabled(home) -> bool` (default `False`).

- [ ] **Step 1: Write the failing tests**

```python
# in the store test file:
def test_write_embedding_with_none_vector_writes_fts_but_not_vec_chunks(tmp_path):
    from mcpbrain.store import Store

    s = Store(tmp_path / "test.db", dim=4)
    s.init()
    s.upsert_chunk("d1", "some table text", "h1", {})
    rowid = s._connect().__enter__().execute(
        "SELECT rowid FROM chunks WHERE doc_id='d1'").fetchone()["rowid"]

    s.write_embedding(rowid, None)

    with s._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM vec_chunks WHERE rowid=?", (rowid,)
        ).fetchone()[0] == 0
        assert db.execute(
            "SELECT COUNT(*) FROM fts_chunks WHERE rowid=?", (rowid,)
        ).fetchone()[0] == 1
        assert db.execute(
            "SELECT embedded FROM chunks WHERE rowid=?", (rowid,)
        ).fetchone()["embedded"] == 1
```

```python
# in a config test file (confirm existing filename with
# `grep -rln "salience_gate_enabled" tests/`):
def test_embed_skip_tabular_defaults_off(tmp_path):
    from mcpbrain import config
    assert config.embed_skip_tabular_enabled(str(tmp_path)) is False
```

```python
# in the index_pending test file:
def test_index_pending_skips_embedder_for_table_chunks(tmp_path, monkeypatch):
    from mcpbrain import config, index
    from mcpbrain.store import Store

    home = tmp_path / "home"
    home.mkdir()
    (home / "config.json").write_text('{"embed_skip_tabular": true}')

    store = Store(tmp_path / "test.db", dim=4)
    store.init()
    store.upsert_chunk("gdrive-t-0", "table row text", "h1",
                       {"content_subtype": "table"})
    store.upsert_chunk("gmail-p-0", "prose text", "h2", {"content_subtype": "prose"})

    class _FakeEmbedder:
        def __init__(self):
            self.calls = []
        def embed_passages(self, texts):
            self.calls.append(list(texts))
            return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    embedder = _FakeEmbedder()
    index.index_pending(store, embedder, home=str(home))

    # Only the prose chunk's text ever reached the embedder.
    all_texts = [t for call in embedder.calls for t in call]
    assert "prose text" in " ".join(all_texts) or any(
        "prose text" in t for t in all_texts)
    assert not any("table row text" in t for t in all_texts)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_store.py -k write_embedding_with_none -v`
Run: `uv run pytest tests/test_config.py -k embed_skip_tabular -v`
Run: `uv run pytest tests/test_index.py -k skips_embedder_for_table -v`
Expected: all `FAIL` — `write_embedding` doesn't accept `None`, `embed_skip_tabular_enabled` doesn't exist, `index_pending` embeds everything.

- [ ] **Step 3: Implement `write_embedding(rowid, vector=None)`**

In `mcpbrain/store.py`, change `write_embedding` (around line 1882):

```python
    def write_embedding(self, rowid: int, vector: list[float] | None, *, home=None) -> None:
        """Write a chunk's embedding and FTS row, stamping embedded=1.

        vector=None means "skip the dense vector, still write FTS and stamp
        embedded=1" -- used for content_subtype=='table' chunks when
        embed_skip_tabular is on. embedded=1 with no vec_chunks row is safe
        everywhere else that gates on embedded=1 for vector search: a join
        against vec_chunks simply returns nothing for that rowid, which is
        exactly the desired "never surfaces via dense search" behavior,
        while unembedded_chunks() correctly stops re-fetching it.
        """
        with self._connect(write=True) as db:
            db.execute("DELETE FROM vec_chunks WHERE rowid=?", (rowid,))
            if vector is not None:
                db.execute("INSERT INTO vec_chunks(rowid, embedding) VALUES(?,?)",
                           (rowid, sqlite_vec.serialize_float32(vector)))
            row = db.execute("SELECT text, metadata FROM chunks WHERE rowid=?",
                             (rowid,)).fetchone()
            fts_text, applied = self._fts_text(row["text"], json.loads(row["metadata"]),
                                                home=home)
            db.execute("DELETE FROM fts_chunks WHERE rowid=?", (rowid,))
            db.execute("INSERT INTO fts_chunks(rowid, text) VALUES(?,?)", (rowid, fts_text))
            db.execute(
                "UPDATE chunks SET embedded=1, fts_context_version=? WHERE rowid=?",
                (self.FTS_CONTEXT_VERSION if applied else 0, rowid))
```

- [ ] **Step 4: Implement `config.embed_skip_tabular_enabled`**

In `mcpbrain/config.py`, near `salience_gate_enabled` (matching its exact docstring style):

```python
def embed_skip_tabular_enabled(home) -> bool:
    """Whether content_subtype=='table' chunks skip the dense-vector embedder.

    When True, index_pending never calls embed_passages for table chunks --
    they still get full FTS indexing (write_embedding(rowid, None) writes
    the FTS row regardless), only the dense vector is skipped. Raw
    markdown/table embeddings underperform schema-enriched row sentences for
    retrieval (RAG-chunking research, 2026-08), and this corpus's own
    salience gate already treats tabular content as low-signal for
    graph-extraction -- this extends that same judgment to dense embedding.

    Default: FALSE. Ships off until validated on the live gold-eval harness
    that recall@10/MRR are unaffected -- same rollout discipline as
    salience_gate and recall_excludes_cold before it.
    """
    return bool(read_config(home).get("embed_skip_tabular", False))
```

- [ ] **Step 5: Implement `index_pending`'s batch partitioning**

In `mcpbrain/index.py`, inside the `for i in range(0, len(pending), batch_size):` loop (around line 60-84):

```python
        use_prefix = config.contextual_retrieval_enabled(_home)
        skip_tabular = config.embed_skip_tabular_enabled(_home)
        for i in range(0, len(pending), batch_size):
            if budget is not None and budget.expired():
                log.info("index_pending: budget spent after %d chunks", done)
                break
            batch = pending[i:i + batch_size]
            if skip_tabular:
                table_batch = [c for c in batch
                              if c["metadata"].get("content_subtype") == "table"]
                normal_batch = [c for c in batch if c not in table_batch]
            else:
                table_batch, normal_batch = [], batch
            texts = [
                (contextual_prefix(c["metadata"]) + c["text"]) if use_prefix else c["text"]
                for c in normal_batch
            ]
            oversize = sum(1 for t in texts if len(t) > EMBED_WINDOW_CHARS)
            if oversize:
                log.warning("index: %d of %d passages exceed the %d-char embedder "
                            "window; their tails will not be searchable",
                            oversize, len(texts), EMBED_WINDOW_CHARS)
            vectors = embedder.embed_passages(texts) if normal_batch else []
            with bulk_section():
                for c, v in zip(normal_batch, vectors):
                    store.write_embedding(c["rowid"], v, home=_home)
                    done += 1
                for c in table_batch:
                    store.write_embedding(c["rowid"], None, home=_home)
                    done += 1
```

(`import config` — check whether `mcpbrain.index` already imports `mcpbrain.config` at module level; the existing `_home = home or str(config.app_dir())` line near the top of `index_pending` confirms it does.)

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_store.py tests/test_config.py tests/test_index.py -q`
Expected: `PASS`.

- [ ] **Step 7: Run the full store/index/config test suites**

Run: `uv run pytest tests/test_store.py tests/test_config.py tests/test_index.py -q`
Expected: `PASS`.

- [ ] **Step 8: Ruff and commit**

```bash
uv run ruff check mcpbrain/store.py mcpbrain/index.py mcpbrain/config.py tests/test_store.py tests/test_config.py tests/test_index.py
git add mcpbrain/store.py mcpbrain/index.py mcpbrain/config.py tests/test_store.py tests/test_config.py tests/test_index.py
git commit -m "feat: skip dense embedding for table content behind embed_skip_tabular

Keeps full FTS indexing (write_embedding(rowid, None) still writes the FTS
row and stamps embedded=1) -- only the weak, research-disfavored raw-table
dense embedding is skipped. Ships off, validate on the gold harness before
flipping on."
```

---

### Task 13: Bump `CHUNKER_VERSION` + one-time `vec_chunks` cleanup script

**Files:**
- Modify: `mcpbrain/chunking.py` (`CHUNKER_VERSION`)
- Create: `bin/cleanup_tabular_vectors.py`
- Test: `tests/test_cleanup_tabular_vectors.py` (new). No hardcoded `CHUNKER_VERSION == 2` pin exists anywhere in the test suite to update — confirmed: `test_chunk_metadata.py:138` asserts `CHUNKER_VERSION >= 2` (safe under a bump to 3), and every `test_ingest_cache*.py`/`test_org_*.py` reference goes through `str(CHUNKER_VERSION)` dynamically rather than a literal — so the bump itself needs no test updates.

**Interfaces:**
- Produces: `mcpbrain.chunking.CHUNKER_VERSION == 3`, `bin/cleanup_tabular_vectors.py` (dry-run default, `--apply` to commit — matching `bin/relocate_ingest_cache.py`'s convention).

**Context:** Bumping the version marks every chunk below it stale, which Task 7-9's generalized sweep now knows how to act on for every source type. The 3,510 existing oversize tabular chunks would otherwise keep polluting dense search until the full re-fetch sweep drains at the daemon's normal backfill pace — this script deletes their `vec_chunks` rows immediately, ahead of that.

- [ ] **Step 1: Check for an existing `CHUNKER_VERSION` pin test**

Run: `grep -rn "CHUNKER_VERSION" tests/*.py`

If a test asserts the exact current value (e.g. `assert CHUNKER_VERSION == 2`), that test's expected failure IS this task's "write the failing test" — update it to expect `3` as this task's Step 3, and treat that update itself as the change under test (a version-pin test doesn't need new test code, just the constant's new value flowing through it).

- [ ] **Step 2: Write the failing test for the cleanup script**

Create `tests/test_cleanup_tabular_vectors.py`:

```python
def test_dry_run_reports_without_deleting(tmp_path, capsys):
    from mcpbrain.store import Store
    import subprocess, sys

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4)
    store.init()
    store.upsert_chunk("gdrive-f1-0", "x" * 3000, "h1", {"content_subtype": "table"})
    rowid = store._connect().__enter__().execute(
        "SELECT rowid FROM chunks WHERE doc_id='gdrive-f1-0'").fetchone()["rowid"]
    store.write_embedding(rowid, [0.1, 0.2, 0.3, 0.4])

    out = subprocess.run(
        [sys.executable, "bin/cleanup_tabular_vectors.py", "--home", str(tmp_path)],
        capture_output=True, text=True)

    assert out.returncode == 0, out.stderr
    assert "1" in out.stdout  # reports 1 candidate
    with store._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM vec_chunks WHERE rowid=?", (rowid,)
        ).fetchone()[0] == 1, "dry run must not delete anything"


def test_apply_deletes_matching_vec_chunks_rows(tmp_path):
    from mcpbrain.store import Store
    import subprocess, sys

    db_path = tmp_path / "brain.sqlite3"
    store = Store(db_path, dim=4)
    store.init()
    store.upsert_chunk("gdrive-f1-0", "x" * 3000, "h1", {"content_subtype": "table"})
    store.upsert_chunk("gdrive-f2-0", "y" * 100, "h2", {"content_subtype": "table"})
    store.upsert_chunk("gmail-m1-0", "z" * 3000, "h3", {"content_subtype": "prose"})
    for doc_id in ("gdrive-f1-0", "gdrive-f2-0", "gmail-m1-0"):
        rowid = store._connect().__enter__().execute(
            "SELECT rowid FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()["rowid"]
        store.write_embedding(rowid, [0.1, 0.2, 0.3, 0.4])

    subprocess.run(
        [sys.executable, "bin/cleanup_tabular_vectors.py", "--home", str(tmp_path),
         "--apply"],
        capture_output=True, text=True, check=True)

    with store._connect() as db:
        remaining = {r["doc_id"] for r in db.execute(
            "SELECT c.doc_id FROM chunks c JOIN vec_chunks v ON v.rowid=c.rowid"
        ).fetchall()}
    # Only the oversize table chunk (f1) is deleted; the short table chunk
    # (f2, under 2000 chars) and the prose chunk (m1) survive.
    assert remaining == {"gdrive-f2-0", "gmail-m1-0"}
```

- [ ] **Step 3: Bump `CHUNKER_VERSION`** (no test updates needed — see the Files note above)

In `mcpbrain/chunking.py`, change `CHUNKER_VERSION = 2` to `CHUNKER_VERSION = 3`, and add a comment referencing why:

```python
# Bumped 3: the tabular renderer redesign (schema-enriched row sentences,
# replacing the fixed-width markdown grid) and normalise_rows' minimum-
# row-support fix both change what a "correctly chunked" table looks like --
# every existing chunk below this version gets re-fetched and re-rendered by
# bin/repair.py's generalized reingest-stale sweep.
CHUNKER_VERSION = 3
```

Update any test asserting the old literal value.

- [ ] **Step 4: Run test to verify the cleanup script fails (doesn't exist yet)**

Run: `uv run pytest tests/test_cleanup_tabular_vectors.py -v`
Expected: `FAIL` — `bin/cleanup_tabular_vectors.py` doesn't exist, subprocess returns non-zero / `FileNotFoundError`.

- [ ] **Step 5: Write `bin/cleanup_tabular_vectors.py`**

```python
#!/usr/bin/env python3
"""One-shot cleanup: delete vec_chunks rows for oversize table-subtype chunks.

The tabular renderer bug (phantom-column-inflated width, fixed in
mcpbrain/sync/tabular.py) produced chunks up to 293KB, each holding a
near-duplicate low-quality vector (title text + garbage) once truncated by
the embedder. The CHUNKER_VERSION bump means every one of these gets
re-fetched and re-rendered by `bin/repair.py reingest-stale` eventually, but
that runs at the daemon's normal backfill pace -- this script deletes the
existing garbage vectors immediately so they stop polluting dense search
right away, ahead of the full re-fetch.

Dry-run by default; pass --apply to actually delete. Matches the
bin/relocate_ingest_cache.py / bin/consolidate.py convention.
"""
import argparse
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--home", default=None,
                    help="MCPBRAIN_HOME override (defaults to config.app_dir())")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; default is dry-run (report only)")
    args = ap.parse_args(argv)

    from pathlib import Path
    from mcpbrain import config
    from mcpbrain.store import Store

    home = args.home or str(config.app_dir())
    store = Store(Path(home) / "brain.sqlite3", dim=4)

    with store._connect() as db:
        rows = db.execute(
            "SELECT rowid, doc_id FROM chunks "
            "WHERE json_extract(metadata,'$.content_subtype')='table' "
            "  AND length(text) > 2000"
        ).fetchall()

    print(f"[cleanup-tabular-vectors] {len(rows)} oversize table chunk(s) found")
    if not args.apply:
        print("[cleanup-tabular-vectors] dry run — nothing deleted; "
              "pass --apply to delete their vec_chunks rows")
        return 0

    with store._connect(write=True) as db:
        for r in rows:
            db.execute("DELETE FROM vec_chunks WHERE rowid=?", (r["rowid"],))
    print(f"[cleanup-tabular-vectors] deleted {len(rows)} vec_chunks row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest tests/test_cleanup_tabular_vectors.py -v`
Expected: `PASS`.

- [ ] **Step 7: Run the full chunking + tabular test suites**

Run: `uv run pytest tests/test_chunking.py tests/test_tabular.py -q`
Expected: `PASS`.

- [ ] **Step 8: Ruff and commit**

```bash
uv run ruff check mcpbrain/chunking.py bin/cleanup_tabular_vectors.py tests/test_cleanup_tabular_vectors.py
git add mcpbrain/chunking.py bin/cleanup_tabular_vectors.py tests/test_cleanup_tabular_vectors.py
git commit -m "feat: bump CHUNKER_VERSION to 3, add one-shot tabular-vector cleanup

The version bump triggers the generalized reingest-stale sweep (Phase 2)
for every chunk below it; the cleanup script deletes existing garbage
vectors from the phantom-column bug immediately, ahead of that sweep's
normal backfill pace."
```

---

## Phase 4 — Consolidated notes (spec §3)

### Task 14: `_write_note` — bound via `chunk_text()`

**Files:**
- Modify: `mcpbrain/consolidation.py` (`_write_note`)
- Test: `tests/test_consolidation.py`

**Interfaces:**
- Produces: `_write_note(store, cluster, summary) -> str | None` — same signature and return (the FIRST doc_id, for single- or multi-chunk cases alike), but writes N doc_ids `note-consolidated-<hash>-<i>` when `chunk_text(summary)` returns more than one piece.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_consolidation.py`:

```python
def test_write_note_short_summary_keeps_single_doc_id(store):
    from mcpbrain.consolidation import _write_note

    cluster = [{"doc_id": "src-1"}]
    doc_id = _write_note(store, cluster, "A short note.")

    assert doc_id == f"note-consolidated-{__import__('hashlib').sha256(b'A short note.').hexdigest()[:16]}"
    with store._connect() as db:
        rows = db.execute("SELECT doc_id FROM chunks WHERE doc_id LIKE ?",
                          (f"{doc_id}%",)).fetchall()
    assert [r["doc_id"] for r in rows] == [doc_id]


def test_write_note_long_summary_splits_into_multiple_chunks(store):
    from mcpbrain.consolidation import _write_note

    cluster = [{"doc_id": "src-1"}]
    long_summary = "This is a sentence about the project. " * 200  # well over 2000 chars

    doc_id = _write_note(store, cluster, long_summary)

    with store._connect() as db:
        rows = db.execute(
            "SELECT doc_id, length(text) as len, metadata FROM chunks "
            "WHERE doc_id LIKE ? ORDER BY doc_id",
            (f"{doc_id}%",)).fetchall()

    assert len(rows) > 1, "a long summary must split into multiple chunks"
    for r in rows:
        assert r["len"] <= 2000
    import json
    metas = [json.loads(r["metadata"]) for r in rows]
    assert [m["chunk_index"] for m in metas] == list(range(len(rows)))
    assert all(m["chunk_total"] == len(rows) for m in metas)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_consolidation.py -k write_note -v`
Expected: the long-summary test `FAIL`s (current code writes one unbounded chunk with no `chunk_index`/`chunk_total` metadata); the short-summary test may already pass — confirm both are meaningful before proceeding.

- [ ] **Step 3: Implement the chunk_text()-bounded write**

Replace `_write_note` in `mcpbrain/consolidation.py`:

```python
def _write_note(store, cluster: list[dict], summary: str) -> str | None:
    """Write a consolidated semantic note, bounded like every other
    multi-chunk source. Returns the FIRST doc_id written, or None.

    The consolidation prompt already asks for "a concise durable semantic
    note (3-6 sentences)" -- and 1,151 existing notes still averaged 18K
    chars anyway. That's why this is a write-time cap, not a prompt fix: an
    LLM instruction is evidence, not a guarantee, and every other bound in
    this codebase is defensive at the write layer for exactly that reason.
    """
    if not summary:
        return None

    from mcpbrain.chunking import chunk_text

    source_ids = [c.get("doc_id", "") for c in cluster]
    ts = _now_iso()
    content_hash_full = hashlib.sha256(summary.encode()).hexdigest()[:16]
    base_doc_id = f"note-consolidated-{content_hash_full}"

    pieces = chunk_text(summary)
    if len(pieces) <= 1:
        doc_id = base_doc_id
        metadata = {
            "observation_type": "consolidated",
            "source_doc_ids": source_ids,
            "captured_at": ts,
            "title": "Consolidated note",
        }
        store.upsert_chunk(doc_id, summary, content_hash_full, metadata)
        store.set_chunk_type(doc_id, "semantic")
        store.set_chunk_tier(doc_id, "hot")
        return doc_id

    first_doc_id = None
    for i, piece in enumerate(pieces):
        doc_id = f"{base_doc_id}-{i}"
        if first_doc_id is None:
            first_doc_id = doc_id
        metadata = {
            "observation_type": "consolidated",
            "source_doc_ids": source_ids,
            "captured_at": ts,
            "title": "Consolidated note",
            "chunk_index": i,
            "chunk_total": len(pieces),
        }
        piece_hash = hashlib.sha256(piece.encode()).hexdigest()[:16]
        store.upsert_chunk(doc_id, piece, piece_hash, metadata)
        store.set_chunk_type(doc_id, "semantic")
        store.set_chunk_tier(doc_id, "hot")
    return first_doc_id
```

(Check that `hashlib` is already imported at the top of `consolidation.py` — it's used elsewhere in the file per the existing `_write_note`'s `hashlib.sha256(summary.encode())` call, so this should already be in scope.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_consolidation.py -k write_note -v`
Expected: `PASS`.

- [ ] **Step 5: Run the full consolidation test suite**

Run: `uv run pytest tests/test_consolidation.py -q`
Expected: `PASS`.

- [ ] **Step 6: Ruff and commit**

```bash
uv run ruff check mcpbrain/consolidation.py tests/test_consolidation.py
git add mcpbrain/consolidation.py tests/test_consolidation.py
git commit -m "fix(consolidation): bound _write_note via chunk_text(), like every other source

The consolidation prompt already asks for 3-6 sentences and 1,151 existing
notes still averaged 18K chars -- a prompt instruction is evidence, not a
guarantee, so the bound has to be defensive at the write layer."
```

---

## Phase 5 — Short-thread prior context (spec §5)

### Task 15: `store.thread_summary_digest` + `prepare._thread_block` fallback

**Files:**
- Modify: `mcpbrain/store.py` (new method)
- Modify: `mcpbrain/prepare.py` (`_thread_block`)
- Modify: `mcpbrain/synthesise_threads.py` (`build_synthesis_requests` — DRY refactor)
- Test: `tests/test_store_schema_p3.py`, `tests/test_prepare.py`, `tests/test_synthesis_contract.py`

**Interfaces:**
- Produces: `Store.thread_summary_digest(thread_id: str, max_chars: int | None = 1500) -> str`.

**Context:** `thread_context.contextual_summary` is populated ONLY by the periodic synthesis pass (threads with `email_count >= 5`). `graph_write.apply()` deliberately never writes it. A thread under 5 messages gets a genuinely empty `prior_thread_context`, even though every message's own one-line `summary` is already durably stored in `email_context`. This is a pure read-side fix — no writer changes.

- [ ] **Step 1: Write the failing test for `thread_summary_digest`**

Add to `tests/test_store_schema_p3.py`:

```python
def test_thread_summary_digest_joins_messages_oldest_first(tmp_path):
    from mcpbrain.store import Store

    s = Store(tmp_path / "test.db", dim=4)
    s.init()
    s.upsert_email_context("m1", thread_id="t1", date_iso="2026-06-01",
                           content_type="request", summary="Joel asked about Hall B.")
    s.upsert_email_context("m2", thread_id="t1", date_iso="2026-06-02",
                           content_type="update", summary="Sam confirmed availability.")

    digest = s.thread_summary_digest("t1")

    lines = digest.split("\n")
    assert lines[0].startswith("- 2026-06-01")
    assert "Joel asked about Hall B." in lines[0]
    assert lines[1].startswith("- 2026-06-02")
    assert "Sam confirmed availability." in lines[1]


def test_thread_summary_digest_drops_oldest_lines_when_over_budget(tmp_path):
    from mcpbrain.store import Store

    s = Store(tmp_path / "test.db", dim=4)
    s.init()
    for i in range(20):
        s.upsert_email_context(f"m{i}", thread_id="t1", date_iso=f"2026-06-{i+1:02d}",
                               content_type="update",
                               summary="A reasonably long summary line " * 5)

    digest = s.thread_summary_digest("t1", max_chars=500)

    assert len(digest) <= 500
    # The MOST RECENT message must survive; an early one must have been dropped.
    assert "2026-06-20" in digest
    assert "2026-06-01" not in digest
```

(Check `upsert_email_context`'s exact keyword-argument list at `mcpbrain/store.py:2277` before finalizing — this test assumes `thread_id`, `date_iso`, `content_type`, `summary` are all valid kwargs, which matches the call already seen in `graph_write.py:1117-1125`.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_store_schema_p3.py -k thread_summary_digest -v`
Expected: `FAIL` — `AttributeError: 'Store' object has no attribute 'thread_summary_digest'`.

- [ ] **Step 3: Implement `thread_summary_digest`**

In `mcpbrain/store.py`, near `thread_context`/`thread_messages`:

```python
    def thread_summary_digest(self, thread_id: str, max_chars: int | None = 1500) -> str:
        """Join a thread's per-message summaries into one digest, oldest
        message first, dropping the OLDEST lines first if it doesn't fit.

        thread_context.contextual_summary is only ever populated by the
        periodic cross-message synthesis pass (threads with email_count>=5).
        A thread under that threshold gets a genuinely empty
        prior_thread_context otherwise, even though every message's own
        one-line summary is already sitting right here in email_context.
        This is the fallback prepare._thread_block reaches for when
        thread_context is empty -- the most recent messages are the most
        relevant prior context for whatever's about to be enriched next, so
        recency wins when trimming to budget, not chronological completeness.

        max_chars=None means no cap (used by build_synthesis_requests, which
        already caps the THREAD count via min_emails/limit rather than the
        digest's own length).
        """
        if not thread_id:
            return ""
        with self._connect() as db:
            rows = db.execute(
                "SELECT date_iso, content_type, summary FROM email_context "
                "WHERE thread_id=? AND summary != '' ORDER BY date_iso",
                (thread_id,),
            ).fetchall()
        lines = [
            f"- {r['date_iso'] or '?'}"
            f"{f' [{r[\"content_type\"]}]' if r['content_type'] else ''}: {r['summary']}"
            for r in rows
        ]
        if max_chars is None:
            return "\n".join(lines)
        # Drop OLDEST lines first (lines is already oldest-to-newest) until
        # the joined result fits.
        while lines and len("\n".join(lines)) > max_chars:
            lines.pop(0)
        return "\n".join(lines)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_store_schema_p3.py -k thread_summary_digest -v`
Expected: `PASS`.

- [ ] **Step 5: Write the failing test for `_thread_block`'s fallback**

Add to `tests/test_prepare.py` (matching the existing `_stub_context`/`FakeStore` fixtures already in this file):

```python
def test_thread_block_falls_back_to_digest_when_thread_context_empty(monkeypatch):
    calls = {"digest": 0}

    class _Store(FakeStore):
        def thread_context(self, thread_id):
            return ""  # not yet synthesized
        def thread_summary_digest(self, thread_id, max_chars=1500):
            calls["digest"] += 1
            return "- 2026-06-01: Joel asked about Hall B."

    store = _Store()
    batch = FakeBatch("t1", ["d1"], [
        _msg("m1", "joel@example.org", "2026-06-01", "Hall B", "text"),
    ])
    _stub_reassemble(monkeypatch)

    block = prepare._thread_block(store, batch)

    assert calls["digest"] == 1
    assert block["prior_thread_context"] == "- 2026-06-01: Joel asked about Hall B."


def test_thread_block_prefers_real_synthesis_over_digest(monkeypatch):
    calls = {"digest": 0}

    class _Store(FakeStore):
        def thread_context(self, thread_id):
            return "A real synthesized narrative."
        def thread_summary_digest(self, thread_id, max_chars=1500):
            calls["digest"] += 1
            return "should never be used"

    store = _Store()
    batch = FakeBatch("t1", ["d1"], [
        _msg("m1", "joel@example.org", "2026-06-01", "Hall B", "text"),
    ])
    _stub_reassemble(monkeypatch)

    block = prepare._thread_block(store, batch)

    assert calls["digest"] == 0
    assert block["prior_thread_context"] == "A real synthesized narrative."
```

(Check `_thread_block`'s exact return-dict key — the spec calls it `prior_thread_context` conversationally, but `mcpbrain/prepare.py:588`'s actual dict key is `"prior_thread_context"` per the function body already read at `prepare.py:586-592` — confirm before finalizing, and adjust `FakeStore`'s existing `thread_context`/if it needs a new `thread_summary_digest` stub method added to the base `FakeStore` class too, so other existing tests using `FakeStore` don't break on a missing attribute.)

- [ ] **Step 6: Run tests to verify they fail**

Run: `uv run pytest tests/test_prepare.py -k thread_block_falls_back -v`
Expected: `FAIL` — current `_thread_block` never calls `thread_summary_digest`, so `calls["digest"] == 0` when it should be `1`.

- [ ] **Step 7: Implement the fallback in `_thread_block`**

In `mcpbrain/prepare.py`'s `_thread_block` (around line 570-575):

```python
    try:
        prior = store.thread_context(batch.thread_id) or ""
    except AttributeError:  # Phase 1 seam: method absent until Phase 1 lands; real errors must surface.
        prior = ""
    if not prior:
        try:
            prior = store.thread_summary_digest(batch.thread_id) or ""
        except AttributeError:
            prior = ""
```

- [ ] **Step 8: Run tests to verify they pass**

Run: `uv run pytest tests/test_prepare.py -k thread_block -v`
Expected: `PASS`.

- [ ] **Step 9: DRY refactor — `build_synthesis_requests` reuses the same digest method**

In `mcpbrain/synthesise_threads.py`, replace the inline per-message-join loop in `build_synthesis_requests` (lines 30-37):

```python
    threads = store.threads_needing_summary(min_emails)[:limit]
    requests = []
    for t in threads:
        email_summaries = store.thread_summary_digest(t["thread_id"], max_chars=None)
        if not email_summaries:
            continue  # skip threads with no per-message summaries
        msgs = store.thread_messages(t["thread_id"])
        first_date = msgs[0].get("date_iso", "?") if msgs else "?"
        last_date = msgs[-1].get("date_iso", "?") if msgs else "?"
        requests.append({
            "thread_id": t["thread_id"],
            "subject": t.get("subject", ""),
            "org": t.get("org", ""),
            "email_count": t.get("email_count", 0),
            "first_date": first_date,
            "last_date": last_date,
            "email_summaries": email_summaries,
        })
    return requests
```

Check `thread_summary_digest`'s line format exactly matches what `test_synthesis_contract.py`'s existing tests expect for `email_summaries` (it used `f"- {date}{ctype}: {summary}"` with `ctype = f" [{content_type}]" if content_type else ""` — confirm `thread_summary_digest`'s format string in Task 15 Step 3 produces byte-identical output for the same input before relying on this refactor; if there's a subtle formatting mismatch, existing synthesis-contract tests will catch it in the next step).

- [ ] **Step 10: Run the full test suites this task touches**

Run: `uv run pytest tests/test_store_schema_p3.py tests/test_prepare.py tests/test_synthesis_contract.py -q`
Expected: `PASS`. If `test_synthesis_contract.py` fails on a formatting mismatch from Step 9, fix `thread_summary_digest`'s line format to match exactly (the digest method is the new canonical source of truth for this format, but it must not silently change what synthesis requests already looked like).

- [ ] **Step 11: Ruff and commit**

```bash
uv run ruff check mcpbrain/store.py mcpbrain/prepare.py mcpbrain/synthesise_threads.py tests/test_store_schema_p3.py tests/test_prepare.py tests/test_synthesis_contract.py
git add mcpbrain/store.py mcpbrain/prepare.py mcpbrain/synthesise_threads.py tests/test_store_schema_p3.py tests/test_prepare.py tests/test_synthesis_contract.py
git commit -m "feat: give short threads real prior-message context

thread_context.contextual_summary is only ever populated by the periodic
synthesis pass (threads with email_count>=5), so a thread under that
threshold got a genuinely empty prior_thread_context on every subsequent
message -- even though every message's own summary was already sitting in
email_context. Pure read-side fix: no writer changes, no risk to synthesis's
existing behavior. build_synthesis_requests now reuses the same digest
method instead of its own inline join loop."
```

---

## Final gate

- [ ] Run the full test suite: `uv run pytest -q`. All tests pass (the one pre-existing, unrelated failure noted earlier in this project — `test_dashboard_and_search_after_loop` — was already fixed in a prior session; if it reappears, that is a regression from this plan and must be investigated before proceeding, not waved through).
- [ ] Run `uv run ruff check mcpbrain/ bin/` clean.
- [ ] Confirm no task skipped the "update the docstring that assumed the old behavior" step where one existed (`stale_chunker_file_ids`'s Gmail-skip reasoning, `CHUNKER_VERSION`'s bump comment).
- [ ] Push the branch and open a PR summarizing all five phases, linking the spec (`docs/superpowers/specs/2026-08-24-ingestion-resilience-and-tabular-rendering-design.md`) and this plan.
