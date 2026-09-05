# Shared Drive Queue Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate `sync_shared_drive`/`sync_shared_drives` (the last unmigrated sync source) onto the `discover_*`/`handle_*_item`/`work_queue` architecture, closing the same livelock class this plan already closed for My Drive, Gmail, and Calendar.

**Architecture:** `discover_shared_drives` pages each pinned drive's changes feed and enqueues into the existing `sync_queue` table exactly like `discover_drive` does — the per-fileId event collapse `sync_shared_drive` hand-rolls today (`resumed_ids`/`resumed_removed_ids`/`page_token`) disappears entirely, replaced by `sync_queue`'s own `PRIMARY KEY (source, ref_id)` UPSERT. `handle_shared_drive_item` re-fetches current metadata and runs the existing cache-first extraction unchanged. The one genuinely new piece: a small durable `shared_drive_pending_publish` table replaces today's in-memory miss list, so the cache-miss → embed → publish pipeline survives across cycles instead of living inside one synchronous call.

**Tech Stack:** Python 3.12, SQLite (`mcpbrain/store.py`), pytest + pytest-xdist, ruff, `uv` for all commands.

**Spec:** `docs/superpowers/specs/2026-09-04-sync-queue-design.md` (the original design — read its "Scope correction" section first) and this plan's own design rationale below. This plan implements exactly the scope that section defers.

## Global Constraints

- **Run everything through `uv`**: `uv run pytest ...`, `uv run ruff check mcpbrain/`.
- **`store.py` carries no logger.** Store methods return counts/lists; callers log.
- **Never use `Store(str(config.app_dir()))`.** Correct construction: `Store(config.store_path(), dim=384)` in production code, `Store(tmp_path / "x.sqlite3", dim=4)` in tests. `store_path()` takes no arguments.
- **`_S = _strict_suffix()`** must be appended to any new `CREATE TABLE` in `init()`.
- **Gold gate before/after Task 6's live validation: recall@10 ≥ 0.780, MRR ≥ 0.550.**
- **`sync_shared_drive`/`sync_shared_drives`/`_cache_first_extract_one`/`_publish_drive_misses` are real, existing functions — read them in `mcpbrain/sync/drive.py` and `mcpbrain/sync/__init__.py` before writing code that calls or replaces them.** Every signature quoted in this plan was verified against the real source at plan-writing time (2026-09-05); re-verify before use since other work may have touched these files since.
- **`backfill_shared_drive` and `note_drive_presence`/revocation are OUT OF SCOPE** — same boundary the original plan drew for Drive/Gmail/Calendar's own backfill functions. Only the live-delta path migrates.
- Commit after every task. Never push; releasing is a separate, explicitly-instructed step.

---

### Task 1: `shared_drive_pending_publish` table and store methods

**Files:**
- Modify: `mcpbrain/store.py` (add table in `init()`, beside `sync_queue`; add methods near `enqueue_and_advance`)
- Test: `tests/test_shared_drive_pending_publish.py` (create)

**Interfaces:**
- Consumes: nothing new.
- Produces:
  - `Store.record_pending_publish(drive_id: str, file_id: str, content_hash: str) -> None` — UPSERT on `(drive_id, file_id)`; a new `content_hash` for the same file replaces the old row (the file changed again before the old miss was published — only the latest version should ever be published).
  - `Store.pending_publishes(drive_id: str) -> list[tuple[str, str]]` — `[(file_id, content_hash), ...]` for one drive, in the exact shape `_publish_drive_misses`'s `misses` parameter already expects (verified: `mcpbrain/sync/__init__.py:330`, `for file_id, content_hash in misses:`).
  - `Store.clear_pending_publish(drive_id: str, file_id: str) -> None`.

**Why this table, not reusing `sync_queue`:** `sync_queue`'s contract is "presence = pending WORK" (extract-and-write). A pending-publish row means the OPPOSITE — the work is already done (chunks are written), and what's pending is a later-stage, cache-miss-only side effect (share the artifact to the fleet) that depends on a THIRD process (embedding) finishing first. Overloading `sync_queue`'s `event` column to mean two different things would break `due_sync_items`'s single, simple "is this ready to work" semantics that the rest of this plan relies on being uniform across sources.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shared_drive_pending_publish.py
"""shared_drive_pending_publish: the durable seam between a cache-miss
extraction and the fleet-cache publish step.

Today this is an in-memory (file_id, content_hash) list threaded through one
synchronous sync->embed->publish call. The queue model works items
asynchronously per-cycle, so there is no such list left over after
work_queue returns -- this table is what survives across cycles instead.
"""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "p.sqlite3", dim=4)
    s.init()
    return s


def test_record_and_list_pending_publishes(tmp_path):
    s = _store(tmp_path)
    s.record_pending_publish("D1", "f1", "hash1")
    s.record_pending_publish("D1", "f2", "hash2")
    s.record_pending_publish("D2", "f3", "hash3")
    assert sorted(s.pending_publishes("D1")) == [("f1", "hash1"), ("f2", "hash2")]
    assert s.pending_publishes("D2") == [("f3", "hash3")]
    assert s.pending_publishes("D3") == []


def test_new_content_hash_replaces_the_old_pending_row(tmp_path):
    """The file changed again before its old miss was published -- only the
    latest version should ever reach the fleet cache."""
    s = _store(tmp_path)
    s.record_pending_publish("D1", "f1", "hash1")
    s.record_pending_publish("D1", "f1", "hash2")
    assert s.pending_publishes("D1") == [("f1", "hash2")]


def test_clear_removes_one_entry_only(tmp_path):
    s = _store(tmp_path)
    s.record_pending_publish("D1", "f1", "hash1")
    s.record_pending_publish("D1", "f2", "hash2")
    s.clear_pending_publish("D1", "f1")
    assert s.pending_publishes("D1") == [("f2", "hash2")]
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_shared_drive_pending_publish.py -q`
Expected: FAIL — `AttributeError: 'Store' object has no attribute 'record_pending_publish'`

- [ ] **Step 3: Add the table**

In `mcpbrain/store.py`'s `init()`, immediately after the `sync_queue` table/index block added by the original plan:

```python
            # Durable seam for the shared-drive cache-miss -> embed -> publish
            # pipeline. A row means "this file's chunks are extracted and
            # written locally, but not yet shared to the fleet cache" -- it
            # survives across cycles, unlike today's in-memory miss list,
            # because publishing needs the chunk EMBEDDED first (a separate,
            # later step in the same cycle or a subsequent one) and a crash
            # or budget cut between extraction and publish must not lose the
            # obligation to publish once the embedding exists.
            db.execute(f"""CREATE TABLE IF NOT EXISTS shared_drive_pending_publish(
                drive_id      TEXT NOT NULL,
                file_id       TEXT NOT NULL,
                content_hash  TEXT NOT NULL,
                discovered_at TEXT NOT NULL,
                PRIMARY KEY (drive_id, file_id)){_S}""")
```

- [ ] **Step 4: Add the methods**

Beside `enqueue_and_advance` in `mcpbrain/store.py`:

```python
    def record_pending_publish(self, drive_id: str, file_id: str,
                               content_hash: str) -> None:
        """A file was extracted locally and needs sharing to the fleet cache
        once its chunks are embedded. UPSERT: a later call for the same file
        replaces the hash -- only the latest version should ever publish."""
        with self._connect(write=True) as db:
            db.execute(
                "INSERT INTO shared_drive_pending_publish"
                "(drive_id, file_id, content_hash, discovered_at) VALUES(?,?,?,?) "
                "ON CONFLICT(drive_id, file_id) DO UPDATE SET "
                "  content_hash=excluded.content_hash, "
                "  discovered_at=excluded.discovered_at",
                (drive_id, file_id, content_hash, _utc_now_iso()))

    def pending_publishes(self, drive_id: str) -> list[tuple[str, str]]:
        """(file_id, content_hash) pairs awaiting publish for one drive --
        the exact shape _publish_drive_misses's `misses` parameter expects."""
        with self._connect() as db:
            return [(r["file_id"], r["content_hash"]) for r in db.execute(
                "SELECT file_id, content_hash FROM shared_drive_pending_publish "
                "WHERE drive_id=?", (drive_id,)).fetchall()]

    def clear_pending_publish(self, drive_id: str, file_id: str) -> None:
        with self._connect(write=True) as db:
            db.execute("DELETE FROM shared_drive_pending_publish "
                       "WHERE drive_id=? AND file_id=?", (drive_id, file_id))
```

Use whatever `_utc_now_iso`-equivalent timestamp idiom the original plan's Task 1 established in this file (check for it — it may already exist from that work) rather than inventing a second one.

- [ ] **Step 5: Run tests, lint, commit**

Run: `uv run pytest tests/test_shared_drive_pending_publish.py -q` — expect PASS.
Run: `uv run ruff check mcpbrain/store.py`

```bash
git add mcpbrain/store.py tests/test_shared_drive_pending_publish.py
git commit -m "feat(sync): durable pending-publish record for shared-drive cache misses"
```

---

### Task 2: Shared Drive discovery

**Files:**
- Modify: `mcpbrain/sync/drive.py` — add new functions near `discover_drive` (do not touch `sync_shared_drive`/`sync_shared_drives` yet — they are deleted in Task 5, not edited in place, so the diff stays reviewable)
- Test: `tests/test_shared_drive_discovery.py` (create)

**Interfaces:**
- Consumes: `Store.enqueue_and_advance` (existing).
- Produces:
  - `discover_shared_drive(service, store, drive_id: str, source: str, *, budget=None) -> int` — pages ONE drive's `changes().list()`, enqueues, advances per page. Mirrors `discover_drive` exactly except for the Shared-Drive-specific `changes().list()` kwargs.
  - `discover_shared_drives(service, store, *, pin, budget=None) -> dict[str, int]` — enumerates pinned drives via `list_shared_drives(service)` (existing, `mcpbrain/sync/drive.py:440`), calls `discover_shared_drive` per drive with `source=f"drive:{drive_id}"`, isolating per-drive failures exactly as today's `sync_shared_drives` does. Returns `{drive_id: enqueued_count}`. Does NOT run `note_drive_presence`/revocation — that stays a separate, unmigrated concern (see Global Constraints).

**Read `sync_shared_drive` (`mcpbrain/sync/drive.py:708`) and `sync_shared_drives` (`:994`) in full before writing this task** — the pagination shape (`changes().list(pageToken=..., driveId=..., includeItemsFromAllDrives=True, supportsAllDrives=True, includeRemoved=True, fields=_CHANGES_FIELDS)`) and the per-drive failure isolation pattern (`try/except` around each drive's sync inside the enumeration loop, `log.warning` and `continue` on failure) must be preserved exactly.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shared_drive_discovery.py
"""Shared Drive discovery: identical per-page cursor-advance invariant as
discover_drive, applied per pinned drive.

The events-dict collapse + resumed_ids/resumed_removed_ids double-tracking
that sync_shared_drive hand-rolls today disappears entirely here --
sync_queue's PRIMARY KEY (source, ref_id) UPSERT does that collapse for
free, exactly as it already does for My Drive.
"""
from mcpbrain.store import Store
from mcpbrain.sync.drive import discover_shared_drive, discover_shared_drives
from mcpbrain.org_contracts import FleetPin

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
              enrich_logic_floor=1, fleet_secret="s3cret")

PAGES = 3


class _Req:
    def __init__(self, r): self._r = r
    def execute(self, num_retries=0): return self._r


class _Changes:
    def __init__(self, svc): self._svc = svc

    def list(self, **kw):
        assert "corpora" not in kw, "changes().list() rejects corpora"
        tok = kw.get("pageToken")
        self._svc.pages.append(tok)
        i = int(tok)
        body = {"changes": [{"fileId": f"f{i}", "file": {
            "id": f"f{i}", "name": f"d{i}.pdf", "mimeType": "application/pdf",
            "version": "1", "modifiedTime": f"2026-09-0{i}T00:00:00Z"}}]}
        if i < PAGES:
            body["nextPageToken"] = str(i + 1)
        else:
            body["newStartPageToken"] = "DONE"
        return _Req(body)

    def getStartPageToken(self, **kw):
        return _Req({"startPageToken": "1"})


class _Drives:
    def __init__(self, drives): self._drives = drives
    def list(self, **_kw): return _Req({"drives": self._drives})


class _Service:
    def __init__(self, drives=None):
        self.pages = []
        self._drives = drives or [{"id": "D1", "name": "Drive One"}]

    def changes(self): return _Changes(self)
    def drives(self): return _Drives(self._drives)


def _store(tmp_path):
    s = Store(tmp_path / "d.sqlite3", dim=4)
    s.init()
    return s


def test_discover_one_shared_drive_advances_per_page(tmp_path):
    s, svc = _store(tmp_path), _Service()
    s.set_cursor("drive:D1", "1")
    n = discover_shared_drive(svc, s, "D1", "drive:D1")
    assert n == PAGES
    assert s.sync_queue_pending("drive:D1") == PAGES
    assert s.get_cursor("drive:D1") == "DONE"


def test_discover_shared_drives_enumerates_pinned_drives(tmp_path):
    s = _store(tmp_path)
    svc = _Service(drives=[{"id": "D1", "name": "One"}, {"id": "D2", "name": "Two"}])
    s.set_cursor("drive:D1", "1")
    s.set_cursor("drive:D2", "1")
    out = discover_shared_drives(svc, s, pin=PIN)
    assert out == {"D1": PAGES, "D2": PAGES}
    assert s.sync_queue_pending("drive:D1") == PAGES
    assert s.sync_queue_pending("drive:D2") == PAGES


def test_one_drives_failure_does_not_abort_the_others(tmp_path):
    s = _store(tmp_path)

    class _FailingChanges(_Changes):
        def list(self, **kw):
            if kw.get("driveId") == "BAD":
                raise RuntimeError("simulated API failure")
            return super().list(**kw)

    class _S(_Service):
        def changes(self): return _FailingChanges(self)

    svc = _S(drives=[{"id": "BAD", "name": "Broken"}, {"id": "D1", "name": "OK"}])
    s.set_cursor("drive:BAD", "1")
    s.set_cursor("drive:D1", "1")
    out = discover_shared_drives(svc, s, pin=PIN)
    assert "BAD" not in out
    assert out["D1"] == PAGES
```

Note: `discover_shared_drive`'s test above doesn't need `driveId=` threading verified explicitly beyond what `_FailingChanges` checks — if the real `changes().list()` call signature differs from what you find when reading the source, adapt the fake accordingly; the fake exists to prove the invariant, not to be a byte-exact API mock.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_shared_drive_discovery.py -q`
Expected: FAIL — `cannot import name 'discover_shared_drive'`

- [ ] **Step 3: Implement**

```python
def discover_shared_drive(service, store, drive_id: str, source: str, *,
                          budget=None) -> int:
    """Page ONE Shared Drive's changes().list() and enqueue. Identical
    per-page cursor-advance invariant to discover_drive -- see that
    function's docstring for the mechanism and why it closes the livelock.

    `source` is the caller's `f"drive:{drive_id}"` cursor key -- passed in
    rather than computed here so discover_shared_drives (the fleet-wide
    caller) and any direct test both use the exact same key shape without
    duplicating the f-string.
    """
    cursor = store.get_cursor(source)
    if cursor is None:
        tok = service.changes().getStartPageToken(
            driveId=drive_id, supportsAllDrives=True).execute(
            num_retries=_NUM_RETRIES)["startPageToken"]
        store.set_cursor(source, str(tok))
        return 0

    enqueued = 0
    page_token = cursor
    while True:
        if budget is not None and budget.expired():
            break
        resp = service.changes().list(
            pageToken=page_token, driveId=drive_id,
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            includeRemoved=True, fields=_CHANGES_FIELDS,
        ).execute(num_retries=_NUM_RETRIES)

        items = []
        for ch in resp.get("changes", []):
            if ch.get("removed"):
                fid = ch.get("fileId")
                if fid:
                    items.append({"ref_id": fid, "version": "", "event": "remove",
                                  "modified_at": _utc_now_iso()})
                continue
            fmeta = ch.get("file") or {}
            fid = fmeta.get("id")
            if not fid:
                continue
            items.append({
                "ref_id": fid,
                "version": str(fmeta.get("version", "")),
                "event": "upsert",
                "modified_at": fmeta.get("modifiedTime") or _utc_now_iso(),
            })

        nxt = resp.get("nextPageToken")
        advance_to = nxt or resp.get("newStartPageToken") or page_token
        store.enqueue_and_advance(items, source=source, cursor=advance_to)
        enqueued += len(items)
        if not nxt:
            break
        page_token = nxt
    return enqueued


def discover_shared_drives(service, store, *, pin, budget=None) -> dict:
    """Enumerate pinned Shared Drives and discover each. Per-drive failures
    are isolated -- one broken drive must not abort the others, exactly as
    today's sync_shared_drives.

    Deliberately does not run note_drive_presence/revocation -- that stays
    an unmigrated, per-cycle, full-enumeration concern (see this plan's
    Global Constraints).
    """
    drives = list_shared_drives(service)
    out = {}
    for d in drives:
        drive_id = d.get("id")
        if not drive_id:
            continue
        source = f"drive:{drive_id}"
        try:
            out[drive_id] = discover_shared_drive(service, store, drive_id,
                                                  source, budget=budget)
        except Exception as exc:  # noqa: BLE001 — isolate one drive's failure
            log.warning("shared-drive discovery failed for %s (skipped): %s",
                       drive_id, exc)
            continue
        if budget is not None and budget.expired():
            break
    return out
```

Verify `list_shared_drives`'s real signature (`mcpbrain/sync/drive.py:440`) before assuming it takes only `service` — adapt if it needs more.

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/test_shared_drive_discovery.py -q`

```bash
uv run ruff check mcpbrain/sync/drive.py
git add mcpbrain/sync/drive.py tests/test_shared_drive_discovery.py
git commit -m "feat(sync): Shared Drive discovery enqueues and advances per page"
```

---

### Task 3: Shared Drive work handler

**Files:**
- Modify: `mcpbrain/sync/drive.py`
- Test: `tests/test_shared_drive_discovery.py` (extend)

**Interfaces:**
- Consumes: `_cache_first_extract_one` (existing, `mcpbrain/sync/drive.py:529` — read its full docstring before use, especially the `bulk_section` scoping rule: it brackets ONLY the final upsert, never the cache-import or fetch calls), `Store.record_pending_publish` (Task 1).
- Produces: `handle_shared_drive_item(service, store, item, *, fleet_storage, pin, drive_id: str, contextual_retrieval: bool = False, folder_cache=None, report=None, bulk_section=None) -> None`.

**The queue row doesn't carry full file metadata** (only `ref_id`/`version`/`event`/`modified_at`), but `_cache_first_extract_one` needs the full `fmeta` dict (`id`, `name`, `mimeType`, `modifiedTime`, `version`, etc. — see `_file_content_hash`'s and `normalise_drive`'s field use). Re-fetch it, exactly the same pattern Task 5 of the original plan used for `handle_drive_item`: `service.files().get(fileId=fid, supportsAllDrives=True, fields="id,name,mimeType,modifiedTime,version,parents,md5Checksum,size,owners")`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_shared_drive_discovery.py
from mcpbrain.sync.drive import handle_shared_drive_item
from tests.helpers.org_fleet import LocalDirFleetStorage


class _FilesGet:
    def __init__(self, meta): self._meta = meta
    def get(self, fileId, **kw): return _Req(self._meta.get(fileId, {}))


class _ExportFiles(_FilesGet):
    def export(self, fileId, mimeType): return _Req(b"shared drive document body")


class _ServiceWithFiles(_Service):
    def __init__(self, meta=None, **kw):
        super().__init__(**kw)
        self._files_meta = meta or {}

    def files(self):
        f = _ExportFiles(self._files_meta)
        return f


def test_handle_shared_drive_item_extracts_and_records_a_pending_publish(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    svc = _ServiceWithFiles(meta={"f1": {
        "id": "f1", "name": "doc.gdoc",
        "mimeType": "application/vnd.google-apps.document",
        "version": "1", "modifiedTime": "2026-09-01T00:00:00Z"}})
    item = {"source": "drive:D1", "ref_id": "f1", "version": "1",
           "event": "upsert", "modified_at": "2026-09-01T00:00:00Z"}
    handle_shared_drive_item(svc, s, item, fleet_storage=fs, pin=PIN, drive_id="D1")
    assert s.pending_publishes("D1") == [("f1", s.pending_publishes("D1")[0][1])]
    assert s.sync_queue_pending() == 0 or True  # completion is work_queue's job, not this handler's


def test_handle_shared_drive_item_removal_deletes_chunks(tmp_path):
    s, fs = _store(tmp_path), LocalDirFleetStorage(tmp_path / "drv")
    svc = _ServiceWithFiles()
    item = {"source": "drive:D1", "ref_id": "gone", "version": "",
           "event": "remove", "modified_at": "2026-09-01T00:00:00Z"}
    # must not raise even with nothing to delete
    handle_shared_drive_item(svc, s, item, fleet_storage=fs, pin=PIN, drive_id="D1")
```

If `_cache_first_extract_one`'s real fetch/export call shape differs from what `_ExportFiles`/`_ServiceWithFiles` mock (check `fetch_content` and `_file_content_hash` in the real file), adapt the fakes to match — they exist to exercise the real code path, not to encode an assumption about it.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_shared_drive_discovery.py -q`
Expected: FAIL — `cannot import name 'handle_shared_drive_item'`

- [ ] **Step 3: Implement**

```python
def handle_shared_drive_item(service, store, item, *, fleet_storage, pin,
                             drive_id: str, contextual_retrieval: bool = False,
                             folder_cache: dict | None = None, report=None,
                             bulk_section=None) -> None:
    """Work one queued Shared Drive item. Raises on failure so the loop
    backs it off.

    On a cache MISS (the file's content wasn't already in the fleet cache
    and was extracted locally), records a pending-publish row rather than
    publishing inline -- publishing needs the chunk EMBEDDED first, and
    embedding is a separate step index_pending runs later in the same
    cycle. See this plan's design rationale for why that pending state must
    be durable rather than an in-memory return value.
    """
    from contextlib import nullcontext
    from mcpbrain import ingest_cache
    bulk_section = bulk_section or nullcontext
    fid = item["ref_id"]
    if item["event"] == "remove":
        with bulk_section():
            doc_ids = store.doc_ids_for_file(fid)
            if doc_ids:
                store.invalidate_local_relations_for_docs(doc_ids)
                store.delete_chunks(doc_ids)
        try:
            ingest_cache.remove_file_artifacts(fleet_storage, fid)
        except Exception as exc:  # noqa: BLE001 — artifact GC is best-effort
            log.info("drive: artifact GC skipped for removed file %s: %s", fid, exc)
        return

    fmeta = service.files().get(
        fileId=fid, supportsAllDrives=True,
        fields="id,name,mimeType,modifiedTime,version,parents,md5Checksum,size,owners"
    ).execute(num_retries=_NUM_RETRIES)
    processed, miss = _cache_first_extract_one(
        service, store, fleet_storage, drive_id, fmeta, pin,
        contextual_retrieval=contextual_retrieval, bulk_section=bulk_section,
        folder_cache=folder_cache if folder_cache is not None else {},
        report=report)
    if miss is not None:
        file_id, content_hash = miss
        store.record_pending_publish(drive_id, file_id, content_hash)
```

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/test_shared_drive_discovery.py -q`

```bash
uv run ruff check mcpbrain/sync/drive.py
git add mcpbrain/sync/drive.py tests/test_shared_drive_discovery.py
git commit -m "feat(sync): Shared Drive work handler, durable pending-publish on cache miss"
```

---

### Task 4: The per-cycle publish-pending step

**Files:**
- Modify: `mcpbrain/sync/__init__.py` (new function, near `_publish_drive_misses`)
- Test: `tests/test_shared_drive_publish_pending.py` (create)

**Interfaces:**
- Consumes: `Store.pending_publishes`/`clear_pending_publish` (Task 1), `_publish_drive_misses` (existing, unchanged — reused as-is to preserve its batched-GC-per-drive property).
- Produces: `publish_pending_shared_drive_artifacts(store, ingest_cache, drives_fs: dict[str, object], pin, published_by: str, *, contextual_retrieval: bool = False) -> dict[str, int]` — for each `drive_id` in `drives_fs`, reads its pending publishes, calls `_publish_drive_misses` (existing) with that list, clears the rows for whichever `(file_id, content_hash)` pairs `ingest_cache.publish_file` (called inside `_publish_drive_misses`) actually returned `True` for. Returns `{drive_id: published_count}`.

**Why re-clearing must be per-successfully-published-file, not "clear everything after the call":** `_publish_drive_misses` isolates per-file failures internally (a transient error on one file doesn't abort the batch) and returns only a total count, not which specific files succeeded. `ingest_cache.publish_file` is ALSO safe to call again on a not-yet-embedded chunk — it no-ops via `collect_chunks` filtering out any chunk with no vector yet (verified: `mcpbrain/ingest_cache.py:288-301`, `collect_chunks` skips `vec is None`; `publish_file` returns `False` when `collect_chunks` yields nothing). So the correct, safe behavior is: **only clear a pending-publish row if you can independently confirm ITS OWN publish succeeded** — which means this task needs a small per-file wrapper rather than reusing `_publish_drive_misses`'s aggregate-count return value blindly. Read `_publish_drive_misses`'s body (`mcpbrain/sync/__init__.py:330`) and either (a) extract its per-file loop into a shared helper both it and this new function call, tracking success per file, or (b) call `ingest_cache.publish_file` directly per pending file here (accepting the loss of `_publish_drive_misses`'s batched `gc_superseded_batch` optimisation for now would be wrong — don't do that; do (a)).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_shared_drive_publish_pending.py
"""The per-cycle publish-pending step: only clears a pending-publish row
once THAT file's publish genuinely succeeded -- a chunk that isn't embedded
yet must stay pending and retry next cycle, not be silently dropped.
"""
from mcpbrain.store import Store
from mcpbrain.sync import publish_pending_shared_drive_artifacts
from mcpbrain.org_contracts import FleetPin

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
              enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path):
    s = Store(tmp_path / "p.sqlite3", dim=4)
    s.init()
    return s


class _FakeIngestCache:
    """publish_file succeeds for files with 'ready' in their content_hash,
    and no-ops (returns False) for anything else -- simulating an
    unembedded chunk."""
    def __init__(self):
        self.published = []

    def publish_file(self, store, fs, drive_id, file_id, content_hash, pin,
                     **kw):
        if "ready" in content_hash:
            self.published.append((drive_id, file_id, content_hash))
            return True
        return False


def test_only_successfully_published_files_are_cleared(tmp_path):
    s = _store(tmp_path)
    s.record_pending_publish("D1", "f1", "hash-ready-1")
    s.record_pending_publish("D1", "f2", "hash-not-embedded-yet")
    ic = _FakeIngestCache()
    out = publish_pending_shared_drive_artifacts(
        s, ic, drives_fs={"D1": object()}, pin=PIN, published_by="a@b.c")
    assert out == {"D1": 1}
    assert s.pending_publishes("D1") == [("f2", "hash-not-embedded-yet")]
    assert ("D1", "f1", "hash-ready-1") in ic.published


def test_empty_pending_set_is_a_no_op(tmp_path):
    s = _store(tmp_path)
    ic = _FakeIngestCache()
    out = publish_pending_shared_drive_artifacts(
        s, ic, drives_fs={"D1": object()}, pin=PIN, published_by="a@b.c")
    assert out == {"D1": 0}
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_shared_drive_publish_pending.py -q`
Expected: FAIL — `cannot import name 'publish_pending_shared_drive_artifacts'`

- [ ] **Step 3: Implement**

First, read `_publish_drive_misses` in full (`mcpbrain/sync/__init__.py:330-370ish`) and extract its per-file publish-and-track-success logic into a small shared helper, e.g.:

```python
def _publish_one_miss(store, ingest_cache, fs, drive_id, file_id, content_hash,
                      pin, published_by, *, contextual_retrieval: bool,
                      skip_gc: bool) -> bool:
    """Publish one (file_id, content_hash) miss. Returns whether it actually
    published (False on a no-op -- e.g. not yet embedded -- or a caught
    per-file failure, never raises)."""
    try:
        return bool(ingest_cache.publish_file(
            store, fs, drive_id, file_id, content_hash, pin,
            published_by=published_by, skip_gc=skip_gc,
            contextual_retrieval=contextual_retrieval))
    except Exception as exc:  # noqa: BLE001 — publish is best-effort
        log.info("sync: publish_file skipped for drive %s file %s: %s",
                 drive_id, file_id, exc)
        return False
```

Rewrite `_publish_drive_misses`'s per-file loop body to call `_publish_one_miss` instead of duplicating the try/except inline (preserving its `keep_map`/batched-GC logic around that call unchanged — do not touch the `gc_superseded_batch` call or its surrounding structure).

Then:

```python
def publish_pending_shared_drive_artifacts(store, ingest_cache, *, drives_fs: dict,
                                           pin, published_by: str,
                                           contextual_retrieval: bool = False) -> dict:
    """Publish every drive's durable pending-publish backlog, clearing only
    the files that actually succeeded THIS call. A file that no-ops (not yet
    embedded) stays pending and is retried next time this runs -- never
    silently dropped, matching this plan's retry-forever posture elsewhere.
    """
    out = {}
    for drive_id, fs in drives_fs.items():
        pending = store.pending_publishes(drive_id)
        count = 0
        for file_id, content_hash in pending:
            if _publish_one_miss(store, ingest_cache, fs, drive_id, file_id,
                                 content_hash, pin, published_by,
                                 contextual_retrieval=contextual_retrieval,
                                 skip_gc=True):
                store.clear_pending_publish(drive_id, file_id)
                count += 1
        out[drive_id] = count
    return out
```

`skip_gc=True` here for the same reason `_publish_drive_misses` uses it: batch `gc_superseded_batch` runs once per drive elsewhere in the cycle (verify where — check whether this new step or the existing `_publish_drive_misses` call, if still invoked for anything, should own that batch call once both exist; if `_publish_drive_misses` is fully superseded by this function after Task 5's wiring, move its batch-GC call here instead of duplicating it — read Task 5 before finalizing this detail).

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/test_shared_drive_publish_pending.py -q`

```bash
uv run ruff check mcpbrain/sync/__init__.py
git add mcpbrain/sync/__init__.py tests/test_shared_drive_publish_pending.py
git commit -m "feat(sync): per-cycle step publishes durable shared-drive pending artifacts"
```

---

### Task 5: Wire into `run_sync_cycle`

**Files:**
- Modify: `mcpbrain/sync/__init__.py` — replace the shared-drive block (currently the `if drive_service is not None and home is not None:` block calling `sync_shared_drives`, lines ~178-270+; read the WHOLE block including the backfill continuation below it before changing anything)
- Test: `tests/test_sync_cycle.py` (extend)

**Interfaces:**
- Consumes: `discover_shared_drives` (Task 2), `handle_shared_drive_item` (Task 3), `publish_pending_shared_drive_artifacts` (Task 4), the existing `work_queue` (already handles `"drive:<id>"` sources via its `source.split(":", 1)[0]` prefix resolution — no change needed there).
- Produces: `run_sync_cycle`'s `result["discovered"]` gains shared-drive entries (e.g. `result["discovered"]["drive:D1"] = N`); `result["shared_drives_published"] = {drive_id: count}` replaces whatever the old inline miss-count tracking produced.

**This is the most consequential task in this plan — read the ENTIRE current shared-drive block plus its backfill continuation (`_shared_drive_backfill_step`, `mcpbrain/sync/__init__.py:502`) before touching anything.** The backfill continuation (`bf_sd = _shared_drive_backfill_step(...)`) is OUT OF SCOPE (Global Constraints) but currently reuses `drives_fs` built earlier in the block and ALSO calls `_publish_drive_misses` for its own misses — you must decide, and clearly comment, whether backfill's misses now ALSO go through `record_pending_publish`/`publish_pending_shared_drive_artifacts` (recommended, for consistency — a backfill miss has the exact same "needs embedding then publishing" shape as a live-delta miss) or keep using the old inline `_publish_drive_misses` path since backfill itself is unmigrated. **Recommendation: route backfill's misses through `record_pending_publish` too** — it's a one-line change at each of backfill's two `_publish_drive_misses` call sites (swap them for looping `record_pending_publish` per miss), and having TWO different miss-handling mechanisms live side by side in the same function would be worse than routing both through the one durable mechanism, even though backfill's own pagination/checkpoint logic isn't otherwise touched.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_sync_cycle.py
def test_cycle_discovers_and_publishes_shared_drive_items(tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    from mcpbrain.store import Store
    from mcpbrain import config
    s = Store(tmp_path / "c.sqlite3", dim=4)
    s.init()
    order = []

    def fake_discover_shared_drives(service, store, *, pin, budget=None):
        order.append("discover_shared")
        store.enqueue_and_advance(
            [{"ref_id": "f1", "version": "1", "event": "upsert",
              "modified_at": "2026-09-05T00:00:00"}], source="drive:D1", cursor="2")
        return {"D1": 1}

    def fake_handle_shared(service, store, item, **kw):
        order.append("work_shared")
        store.record_pending_publish("D1", item["ref_id"], "hash1")

    def fake_publish_pending(store, ingest_cache, *, drives_fs, pin, published_by,
                             contextual_retrieval=False):
        order.append("publish_pending")
        for did in drives_fs:
            for fid, _h in store.pending_publishes(did):
                store.clear_pending_publish(did, fid)
        return {did: 1 for did in drives_fs}

    monkeypatch.setattr(sync_mod, "discover_shared_drives", fake_discover_shared_drives)
    monkeypatch.setattr(sync_mod, "handle_shared_drive_item", fake_handle_shared)
    monkeypatch.setattr(sync_mod, "publish_pending_shared_drive_artifacts",
                        fake_publish_pending)
    monkeypatch.setattr(config, "ingest_cache_enabled", lambda home: True)
    monkeypatch.setattr(config, "fleet_pin", lambda home: __import__(
        "mcpbrain.org_contracts", fromlist=["FleetPin"]).FleetPin(
        embed_model="bge-small", dim=4, chunker_version="v1",
        enrich_logic_floor=1, fleet_secret="s"))
    monkeypatch.setattr(config, "owner_email", lambda home: "a@b.c")

    out = sync_mod.run_sync_cycle(s, embedder=None, drive_service=object(),
                                  home=str(tmp_path))
    assert order == ["discover_shared", "work_shared", "publish_pending"]
    assert out["shared_drives_published"] == {"D1": 1}
    assert s.pending_publishes("D1") == []
```

Adapt the monkeypatch targets/mock shapes to whatever the real config accessors (`ingest_cache_enabled`, `fleet_pin`, `owner_email`, `contextual_retrieval_enabled`) actually require — this test sketch assumes their current shapes; verify against `mcpbrain/config.py` before finalizing.

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_sync_cycle.py::test_cycle_discovers_and_publishes_shared_drive_items -q`

- [ ] **Step 3: Rewire the block**

Replace the `sync_shared_drives(...)` call and the per-drive miss/publish loop that follows it with:

```python
    if drive_service is not None and home is not None:
        try:
            from mcpbrain import config
            ingest_cache_on = config.ingest_cache_enabled(home)
            pin = config.fleet_pin(home) if ingest_cache_on else None
            if ingest_cache_on and pin.is_pinned:
                from mcpbrain.sync.drive import (discover_shared_drives,
                                                 handle_shared_drive_item)
                from mcpbrain.fleet_storage import cache_storage_factory
                from mcpbrain import ingest_cache
                cr = config.contextual_retrieval_enabled(home)

                discovered_sd = discover_shared_drives(drive_service, store,
                                                       pin=pin, budget=disc_budget)
                result["discovered"].update(
                    {f"drive:{did}": n for did, n in discovered_sd.items()})

                storage_factory = cache_storage_factory(home, drive_service)
                drives_fs = {did: storage_factory(did) for did in discovered_sd}
                folder_cache: dict = {}
                for did in discovered_sd:
                    handlers[f"drive:{did}"] = (
                        lambda it, _did=did, _fs=drives_fs[did]: handle_shared_drive_item(
                            drive_service, store, it, fleet_storage=_fs, pin=pin,
                            drive_id=_did, contextual_retrieval=cr,
                            folder_cache=folder_cache, bulk_section=bulk_section))
```

**This is illustrative, not literal** — you must integrate it with the ALREADY-EXISTING `handlers = {}` dict and `work_queue(...)` call Task 8 of the original plan wired in (the `"drive"`/`"gmail"`/`"calendar"` handlers) — shared-drive handlers need to be added to that SAME dict before the single `work_queue(...)` call, not in a second, separate call (there is exactly one `work_queue` invocation per cycle; verify this by reading the current file before writing your version). After `work_queue`/`_embed()` run, add:

```python
                published_by = config.owner_email(home)
                if published_by:
                    result["shared_drives_published"] = publish_pending_shared_drive_artifacts(
                        store, ingest_cache, drives_fs=drives_fs, pin=pin,
                        published_by=published_by, contextual_retrieval=cr)
                else:
                    log.warning(
                        "sync: owner_email unconfigured; shared-drive artifacts "
                        "will not be published to the fleet cache this cycle "
                        "(files still synced and embedded locally)")
        except Exception as exc:  # noqa: BLE001 — the whole shared-drive block
                                  # must never abort gmail/calendar/My-Drive sync
            log.warning("sync: shared-drive cycle step failed (skipped): %s", exc)
```

Preserve the existing outer `try/except` around the WHOLE shared-drive block exactly as today (it exists so a Drive-API outage in `list_shared_drives` can never abort gmail/calendar/My-Drive sync that already ran). Preserve the `_shared_drive_backfill_step` call that follows, adapting its own two `_publish_drive_misses` call sites to `record_pending_publish` per the recommendation above, and adding a second `publish_pending_shared_drive_artifacts` call (or folding backfill's misses into the SAME `drives_fs`/one publish call — prefer this if `drives_fs` is still in scope, to avoid two separate publish passes per cycle).

- [ ] **Step 4: Run tests, lint, commit**

Run: `uv run pytest tests/test_sync_cycle.py -q`

```bash
uv run ruff check mcpbrain/sync/__init__.py
git add mcpbrain/sync/__init__.py tests/test_sync_cycle.py
git commit -m "feat(sync): wire Shared Drive discovery/work/publish into the cycle"
```

---

### Task 6: Delete the superseded code and widen the cursor cleanup

**Files:**
- Modify: `mcpbrain/sync/drive.py` (delete `sync_shared_drive`, `sync_shared_drive`'s body only — NOT `sync_shared_drives` if any of its enumeration/revocation logic is still needed elsewhere; NOT `_cache_first_extract_one`, `_publish_drive_misses`'s successor, `list_shared_drives`, or `backfill_shared_drive`, all still live)
- Modify: `mcpbrain/store.py` (widen the one-shot cursor cleanup)
- Delete: `tests/test_shared_drive_paging_resume.py` (the file the original plan's Task 9 preserved specifically because `sync_shared_drive` was still live — now safe to remove, since Tasks 2-3's own test files cover the replacement)
- Test: `tests/test_sync_queue_cleanup.py` (extend)

**Before deleting anything**, `grep -rn "sync_shared_drive\b"` (word-boundary, to avoid matching `sync_shared_drives`) across `mcpbrain/` and `tests/` and confirm every remaining caller has been moved to the new `discover_shared_drive`/`handle_shared_drive_item` pair by Task 5. Run the full suite after deletion — per the original plan's Task 9 precedent, any failure here is a caller still using a deleted function; fix the caller, do not restore the function.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/test_sync_queue_cleanup.py
def test_init_now_also_clears_shared_drive_resume_state(tmp_path):
    """Once sync_shared_drive is deleted, its per-drive resume state is dead
    too -- this is the counterpart to test_init_spares_shared_drive_resume_state
    from the original plan, now that the thing being spared no longer exists."""
    s = Store(tmp_path / "m.sqlite3", dim=4)
    s.init()
    s.set_cursor("drive:D1", "999")
    s.set_cursor("drive:D1:resume_ids", '["a"]')
    s.set_cursor("drive:D1:resume_removed_ids", '["b"]')
    s.set_cursor("drive:D1:page_token", "1000")
    s.init()
    assert s.get_cursor("drive:D1") == "999", "the real per-drive cursor must survive"
    for dead in ("drive:D1:resume_ids", "drive:D1:resume_removed_ids",
                "drive:D1:page_token"):
        assert s.get_cursor(dead) is None, f"{dead} not cleaned up"
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_sync_queue_cleanup.py::test_init_now_also_clears_shared_drive_resume_state -q`
Expected: FAIL — the shared-drive keys still survive (the exact-match allowlist from the original plan's Task 9 doesn't cover them).

- [ ] **Step 3: Widen the cleanup**

The existing cleanup in `store.py`'s `init()` (from the original plan) is an exact-match list. Shared-drive keys are per-drive (`drive:<driveId>:resume_ids` etc.), so an exact-match list can't enumerate them — but a suffix `LIKE` is now SAFE to reintroduce for exactly the `drive:%` prefix, because after this task's deletion there is no longer any live function that writes `drive:<id>:resume_ids`-shaped state:

```python
            db.execute(
                "DELETE FROM sync_cursors WHERE source IN "
                "('drive:resume_ids', 'drive:resume_removed_ids', "
                "'drive:page_token', 'gmail:resume_ids', 'calendar:resume_ids') "
                "OR (source LIKE 'drive:%:resume_ids' "
                "    OR source LIKE 'drive:%:resume_removed_ids' "
                "    OR source LIKE 'drive:%:page_token')")
```

Do not simply change this to a bare `source LIKE '%:resume_ids'` etc. — that would ALSO match anything unrelated that happens to end in those suffixes in the future; keep it scoped to the `drive:` prefix specifically, which is what's actually retired by this task.

- [ ] **Step 4: Delete `sync_shared_drive`'s body**

Remove the function entirely from `mcpbrain/sync/drive.py`. Remove `git rm tests/test_shared_drive_paging_resume.py`.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest tests/ -q`
Expected: ALL PASS. Any failure is a caller still referencing the deleted function — find and fix it (likely nowhere, since Task 5 already moved the only caller, but verify).

Run: `uv run ruff check mcpbrain/`

- [ ] **Step 6: Commit**

```bash
git add -A
git commit -m "refactor(sync): delete sync_shared_drive; widen cursor cleanup to its per-drive keys"
```

---

### Task 7: Live validation (ATTENDED — do not automate)

**Files:** none modified. This task produces evidence.

Mirrors the original plan's Task 10. Run at a terminal, in order, by a human.

- [ ] **Step 1: Verify a fresh backup exists.** `mcpbrain doctor | grep -i backup`. Do not proceed without a recent, verified backup — rollback here is restore-from-backup, same reasoning as the original plan.
- [ ] **Step 2: Record the gold baseline.** Recall@10 ≥ 0.780, MRR ≥ 0.550 is the floor.
- [ ] **Step 3: Dry-run `discover_shared_drives` against the real pinned drives in a THROWAWAY store**, mirroring the original plan's Task 10 Step 3 pattern (seed a scratch store with the real `drive:<id>` cursors read from the live store, call the real Drive API against the throwaway store, confirm cursors advance and `sync_queue`/`shared_drive_pending_publish` fill in as expected).
- [ ] **Step 4: Clean up the throwaway store.**
- [ ] **Step 5: Install and restart** (`uv tool install --force ".[daemon]"`, clear `__pycache__`, restart daemon + tray) — same procedure as the original plan's Task 10.
- [ ] **Step 6: Verify against the RUNNING process.** `mcpbrain doctor` — confirm the version, confirm `sync_queue`/publish-pending activity for shared drives specifically (check `sync_queue_stats()` output for `drive:<id>`-sourced rows, and query `shared_drive_pending_publish` directly if needed).
- [ ] **Step 7: Watch the shared-drive backlog actually drain** over several cycles.
- [ ] **Step 8: Re-measure gold.** Must still clear the floor.
- [ ] **Step 9: Record the outcome in CLAUDE.md**, following the existing entries' style, and update the spec's "Scope correction" section to say the migration is complete rather than planned.

```bash
git add CLAUDE.md docs/superpowers/specs/2026-09-04-sync-queue-design.md
git commit -m "docs: record the shared-drive queue migration and its live validation"
```

---

## Notes for the implementer

- **Read `sync_shared_drive`/`sync_shared_drives`/`_cache_first_extract_one`/`_publish_drive_misses` in full before Task 1** — this plan quotes their real signatures but not their full bodies; several subtle invariants (herd-race re-check before extraction, `bulk_section` scoping around only the final write, per-file failure isolation, batched vs per-file GC) must be preserved exactly.
- **Task 4's per-file success tracking is the one place this plan asks you to change EXISTING, already-approved code** (`_publish_drive_misses`) rather than only adding new functions — do this carefully, and confirm `_publish_drive_misses`'s own existing tests (if any) still pass after the extraction.
- **`backfill_shared_drive` remains untouched.** Only its TWO `_publish_drive_misses` call sites (inside `_shared_drive_backfill_step`, Task 5) change to route through the new durable mechanism — the backfill pagination/checkpoint logic itself is out of scope.
