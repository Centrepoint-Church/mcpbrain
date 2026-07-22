# Centralize Ingest Cache into the Backups Drive — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Relocate the shared-drive ingest cache from a `.mcpbrain-cache/` folder in every team drive's root to a single central location (`<fleet folder>/ingest-cache/<source_drive_id>/.mcpbrain-cache/…`) inside the "MCPBrain Backups" shared drive, so team drives get no mcpbrain folders.

**Architecture:** The per-drive scoping comes entirely from where the `fleet_storage` object is rooted plus the fixed `CACHE_DIR` prefix — so `ingest_cache.py` is not touched. We add an optional `base_path` to `DriveFleetStorage`, a `centralized_cache_storage` factory that roots at the fleet folder with `base_path=ingest-cache/<source_drive_id>`, and a `cache_storage_factory` that picks central-vs-in-drive. Both call sites (`sync/__init__.py`, `onboarding.py`) go through the one factory; it falls back to in-drive automatically if no fleet folder resolves. A one-shot `bin/` script removes the legacy folders.

**Tech Stack:** Python 3, Google Drive API (`googleapiclient`), pytest. In-memory `FakeDrive` double for storage tests (already in `tests/test_fleet_storage_drive.py`).

## Global Constraints

- `ingest_cache.py` MUST NOT be modified — relocation is achieved purely by changing what storage each drive is handed.
- `base_path=""` (the default) MUST be a behavioural no-op — existing `fleet_folder_storage` / `drive_cache_storage` callers are unaffected.
- New config flag `ingest_cache_central` defaults **True**; org-config-flippable via `org-config.json`.
- Every Drive `.execute()` in `DriveFleetStorage` already passes `num_retries=5` — do not remove that.
- All Drive calls set `supportsAllDrives=True` / `includeItemsFromAllDrives=True` (Shared Drives require it).
- The cleanup script (`bin/relocate_ingest_cache.py`) is **dry-run by default**; deletion only on `--delete-legacy`, and must be run only AFTER the whole fleet is on the new wheel (documented in its header).
- Scope test runs to edited + directly-impacted files (Josh runs the full suite himself). Do NOT push or release — that is a separate, explicitly-authorized step.
- Spec: `docs/superpowers/specs/2026-07-22-centralize-ingest-cache-design.md`.

## File Structure

- `mcpbrain/fleet_storage.py` — MODIFY: add `base_path` to `DriveFleetStorage`; add `fleet_folder_id`, `centralized_cache_storage`, `cache_storage_factory`; refactor `fleet_folder_storage` to reuse `fleet_folder_id`.
- `mcpbrain/config.py` — MODIFY: add `ingest_cache_central(home)`.
- `mcpbrain/sync/__init__.py` — MODIFY: swap the inline `storage_factory` lambda for `cache_storage_factory(home, drive_service)`.
- `mcpbrain/onboarding.py` — MODIFY: thread `home` into `_default_make_drive_storage` and route it through `cache_storage_factory`.
- `bin/relocate_ingest_cache.py` — CREATE: one-shot legacy-folder cleanup.
- Tests: `tests/test_fleet_storage_drive.py` (MODIFY), `tests/test_config*` or inline in fleet-storage test (MODIFY), `tests/test_sync_cycle.py` (MODIFY), `tests/test_onboarding_bootstrap.py` (MODIFY), `tests/test_relocate_ingest_cache.py` (CREATE).

---

### Task 1: `base_path` support in `DriveFleetStorage`

Add an optional `base_path` prepended to every resolved path, so an instance behaves as if rooted at `<root>/<base_path>`. Because `list_paths` builds returned paths from the caller's `prefix` (never from a root-walk), `base_path` never leaks into results — no explicit strip is needed.

**Files:**
- Modify: `mcpbrain/fleet_storage.py` (`DriveFleetStorage.__init__` ~line 37; `_resolve_parent` ~line 186)
- Test: `tests/test_fleet_storage_drive.py`

**Interfaces:**
- Produces: `DriveFleetStorage(drive_service, folder_or_drive_id, *, root_is_drive=False, base_path="", ensure_folder_retry_attempts=3, ensure_folder_retry_backoff=0.05)`. New attribute `self._base_parts: list[str]`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fleet_storage_drive.py` (uses the existing `FakeDrive` in that file):

```python
def test_base_path_prepends_on_put_and_get():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT", base_path="ingest-cache/D1")
    fs.put_bytes(".mcpbrain-cache/FID.h.pf.mbc.gz", b"payload")
    assert fs.get_bytes(".mcpbrain-cache/FID.h.pf.mbc.gz") == b"payload"
    # physical tree: ROOT > ingest-cache > D1 > .mcpbrain-cache > FID...
    names = {n["name"] for n in drive.nodes.values()}
    assert {"ingest-cache", "D1", ".mcpbrain-cache", "FID.h.pf.mbc.gz"} <= names


def test_base_path_list_paths_returns_caller_relative():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT", base_path="ingest-cache/D1")
    fs.put_bytes(".mcpbrain-cache/A.mbc.gz", b"a")
    fs.put_bytes(".mcpbrain-cache/B.mbc.gz", b"b")
    # base_path must NOT appear in returned paths
    assert fs.list_paths(".mcpbrain-cache/") == [
        ".mcpbrain-cache/A.mbc.gz", ".mcpbrain-cache/B.mbc.gz"]


def test_base_path_isolates_two_source_drives():
    drive = FakeDrive()
    fa = DriveFleetStorage(drive, "ROOT", base_path="ingest-cache/A")
    fb = DriveFleetStorage(drive, "ROOT", base_path="ingest-cache/B")
    fa.put_bytes(".mcpbrain-cache/x.mbc.gz", b"a")
    fb.put_bytes(".mcpbrain-cache/x.mbc.gz", b"b")
    assert fa.get_bytes(".mcpbrain-cache/x.mbc.gz") == b"a"
    assert fb.get_bytes(".mcpbrain-cache/x.mbc.gz") == b"b"
    assert fa.list_paths(".mcpbrain-cache/") == [".mcpbrain-cache/x.mbc.gz"]


def test_base_path_default_is_noop():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    fs.put_bytes(".mcpbrain-cache/x.mbc.gz", b"p")
    assert "ingest-cache" not in {n["name"] for n in drive.nodes.values()}
    assert fs.get_bytes(".mcpbrain-cache/x.mbc.gz") == b"p"


def test_base_path_read_miss_when_base_absent():
    fs = DriveFleetStorage(FakeDrive(), "ROOT", base_path="ingest-cache/D1")
    assert fs.get_bytes(".mcpbrain-cache/nope.mbc.gz") is None
    assert fs.list_paths(".mcpbrain-cache/") == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fleet_storage_drive.py -k base_path -v`
Expected: FAIL — `DriveFleetStorage.__init__() got an unexpected keyword argument 'base_path'`.

- [ ] **Step 3: Add the `base_path` param**

In `mcpbrain/fleet_storage.py`, extend `__init__` (currently ends its keyword params with the two `ensure_folder_retry_*` knobs). Add `base_path: str = ""` and store parsed parts:

```python
    def __init__(self, drive_service, folder_or_drive_id: str, *,
                 root_is_drive: bool = False,
                 base_path: str = "",
                 ensure_folder_retry_attempts: int = 3,
                 ensure_folder_retry_backoff: float = 0.05):
        self._svc = drive_service
        self._root = folder_or_drive_id
        self._root_is_drive = root_is_drive
        # Folder path prepended to EVERY resolved path, so this instance behaves
        # as if rooted at <root>/<base_path>. list_paths builds returned paths
        # from the caller's prefix (not a root-walk), so base_path never leaks
        # into results — no explicit strip needed.
        self._base_parts = [p for p in base_path.split("/") if p]
        self._ensure_folder_retry_attempts = max(1, ensure_folder_retry_attempts)
        self._ensure_folder_retry_backoff = ensure_folder_retry_backoff
        self._folder_cache: dict[tuple[str, str], str] = {}
```

- [ ] **Step 4: Prepend `base_parts` in `_resolve_parent`**

Replace the loop header in `_resolve_parent` so every resolution (put/get/delete via `_resolve_file`, and `list_paths`) starts by walking the base path:

```python
    def _resolve_parent(self, components: list[str], *, create: bool):
        parent = self._root
        for comp in self._base_parts + list(components):
            if create:
                parent = self._ensure_folder(parent, comp)
            else:
                fid = self._find_child(parent, comp, folder=True)
                if fid is None:
                    return None
                parent = fid
        return parent
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_fleet_storage_drive.py -k base_path -v`
Expected: PASS (all 5).

- [ ] **Step 6: Run the whole storage suite to confirm no regressions**

Run: `pytest tests/test_fleet_storage_drive.py -v`
Expected: PASS (existing tests + the 5 new ones) — confirms `base_path=""` is a true no-op.

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/fleet_storage.py tests/test_fleet_storage_drive.py
git commit -m "feat(fleet_storage): base_path prefix for DriveFleetStorage"
```

---

### Task 2: Config flag + centralized cache factory

Add the `ingest_cache_central` flag and the fleet-folder-rooted factory chain.

**Files:**
- Modify: `mcpbrain/config.py` (add `ingest_cache_central`, next to `ingest_cache_enabled` ~line 838)
- Modify: `mcpbrain/fleet_storage.py` (add `fleet_folder_id`, `centralized_cache_storage`, `cache_storage_factory`; refactor `fleet_folder_storage`)
- Test: `tests/test_fleet_storage_drive.py`

**Interfaces:**
- Consumes: `DriveFleetStorage(..., base_path=...)` from Task 1; existing `drive_cache_storage(drive_service, drive_id)`.
- Produces:
  - `config.ingest_cache_central(home) -> bool`
  - `fleet_storage.fleet_folder_id(home) -> str | None`
  - `fleet_storage.centralized_cache_storage(drive_service, fleet_folder_id_, source_drive_id) -> DriveFleetStorage`
  - `fleet_storage.cache_storage_factory(home, drive_service) -> Callable[[str], FleetStorage]`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_fleet_storage_drive.py`:

```python
def test_ingest_cache_central_defaults_true(tmp_path):
    from mcpbrain import config
    assert config.ingest_cache_central(str(tmp_path)) is True
    config.write_config(str(tmp_path), {"ingest_cache_central": False})
    assert config.ingest_cache_central(str(tmp_path)) is False


def test_fleet_folder_id_prefers_config_then_default(tmp_path):
    from mcpbrain import config, fleet_storage, org_defaults
    assert fleet_storage.fleet_folder_id(str(tmp_path)) == org_defaults.FLEET_FOLDER_ID
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FF"}})
    assert fleet_storage.fleet_folder_id(str(tmp_path)) == "FF"


def test_centralized_cache_storage_roots_at_fleet_folder_with_base_path(tmp_path):
    from mcpbrain import fleet_storage, ingest_cache
    drive = FakeDrive()
    fs = fleet_storage.centralized_cache_storage(drive, "FF", "D1")
    assert fs._root == "FF"
    assert fs._root_is_drive is False          # folder root, not a drive root
    assert fs._base_parts == ["ingest-cache", "D1"]
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/FID.h.pf.mbc.gz", b"p")
    names = {n["name"] for n in drive.nodes.values()}
    assert {"ingest-cache", "D1", ingest_cache.CACHE_DIR} <= names


def test_cache_storage_factory_central_when_flag_on(tmp_path):
    from mcpbrain import config, fleet_storage
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FF"}})
    factory = fleet_storage.cache_storage_factory(str(tmp_path), FakeDrive())
    fs = factory("D1")
    assert fs._root == "FF" and fs._base_parts == ["ingest-cache", "D1"]


def test_cache_storage_factory_in_drive_when_flag_off(tmp_path):
    from mcpbrain import config, fleet_storage
    config.write_config(str(tmp_path), {"ingest_cache_central": False})
    factory = fleet_storage.cache_storage_factory(str(tmp_path), FakeDrive())
    fs = factory("D1")
    assert fs._root == "D1" and fs._root_is_drive is True and fs._base_parts == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_fleet_storage_drive.py -k "central or fleet_folder_id" -v`
Expected: FAIL — `module 'mcpbrain.config' has no attribute 'ingest_cache_central'` (and the fleet_storage functions missing).

- [ ] **Step 3: Add the config flag**

In `mcpbrain/config.py`, immediately after `ingest_cache_enabled` (~line 841):

```python
def ingest_cache_central(home) -> bool:
    """Store the shared-drive ingest cache CENTRALLY under the fleet folder
    (inside the Backups drive) instead of in each source drive's own
    .mcpbrain-cache/. Default True. Org-config-flippable via org-config.json.
    Falls back to in-drive automatically if no fleet folder resolves."""
    return bool(read_config(home).get("ingest_cache_central", True))
```

- [ ] **Step 4: Add `fleet_folder_id` and refactor `fleet_folder_storage`**

In `mcpbrain/fleet_storage.py`, in the factories section (~line 345), add `fleet_folder_id` and make `fleet_folder_storage` reuse it (DRY — single source of truth for the lookup):

```python
def fleet_folder_id(home) -> str | None:
    """The fleet folder id used to root fleet-folder + centralized-cache storage:
    config fleet.folder_id, else the baked-in org default. None only if neither
    resolves (in practice the org default is always set)."""
    from mcpbrain import config, org_defaults
    fleet = config.read_config(home).get("fleet") or {}
    return fleet.get("folder_id") or org_defaults.FLEET_FOLDER_ID or None


def fleet_folder_storage(home, drive_service=None):
    """FleetStorage over the fleet FOLDER (org-graph snapshot / contrib). Returns
    None when there is no drive_service or no folder id resolves."""
    if drive_service is None:
        return None
    folder_id = fleet_folder_id(home)
    if not folder_id:
        return None
    return DriveFleetStorage(drive_service, folder_id)
```

(Replace the existing `fleet_folder_storage` body, which inlined the `config`/`org_defaults` lookup, with this version.)

- [ ] **Step 5: Add `centralized_cache_storage` and `cache_storage_factory`**

In `mcpbrain/fleet_storage.py`, after `drive_cache_storage` (~line 366):

```python
def centralized_cache_storage(drive_service, fleet_folder_id_, source_drive_id):
    """FleetStorage for one Shared Drive's ingest cache, stored CENTRALLY under the
    fleet folder at ingest-cache/<source_drive_id>/. ingest_cache addresses the
    .mcpbrain-cache/ subfolder via CACHE_DIR, so the physical path becomes
    <fleet_folder>/ingest-cache/<source_drive_id>/.mcpbrain-cache/<file>. Rooted at
    the fleet FOLDER (root_is_drive=False, like fleet_folder_storage)."""
    return DriveFleetStorage(drive_service, fleet_folder_id_,
                             base_path=f"ingest-cache/{source_drive_id}")


def cache_storage_factory(home, drive_service):
    """Return storage_factory(source_drive_id) -> FleetStorage for the ingest cache.
    Central (fleet-folder-rooted) when config.ingest_cache_central is on AND a fleet
    folder resolves; otherwise the legacy in-drive storage (drive_cache_storage).
    Safe degradation: an unresolvable fleet folder falls back to in-drive."""
    from mcpbrain import config
    if config.ingest_cache_central(home):
        ffid = fleet_folder_id(home)
        if ffid:
            return lambda d: centralized_cache_storage(drive_service, ffid, d)
    return lambda d: drive_cache_storage(drive_service, d)
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `pytest tests/test_fleet_storage_drive.py -k "central or fleet_folder_id" -v`
Expected: PASS (5 new tests).

- [ ] **Step 7: Confirm the `fleet_folder_storage` refactor didn't regress**

Run: `pytest tests/test_fleet_storage_drive.py -k fleet_folder_storage -v`
Expected: PASS (existing `test_fleet_folder_storage_*` tests still green).

- [ ] **Step 8: Commit**

```bash
git add mcpbrain/config.py mcpbrain/fleet_storage.py tests/test_fleet_storage_drive.py
git commit -m "feat(fleet_storage): centralized cache factory + ingest_cache_central flag"
```

---

### Task 3: Wire both call sites through `cache_storage_factory`

Route the daemon sync path and the onboarding bootstrap path through the new factory.

**Files:**
- Modify: `mcpbrain/sync/__init__.py` (import ~line 85; `storage_factory=` ~line 95)
- Modify: `mcpbrain/onboarding.py` (`_default_make_drive_storage` ~line 70; its caller in `run_bootstrap` ~line 248)
- Test: `tests/test_sync_cycle.py`, `tests/test_onboarding_bootstrap.py`

**Interfaces:**
- Consumes: `fleet_storage.cache_storage_factory(home, drive_service)` from Task 2.
- Produces: `onboarding._default_make_drive_storage(home, drive_service) -> Callable[[str], FleetStorage | None]` (note the NEW leading `home` param).

- [ ] **Step 1: Write/So update the failing tests**

In `tests/test_onboarding_bootstrap.py`, **replace** `test_default_make_drive_storage_builds_per_drive` (currently ~line 212, and it calls the old one-arg signature) with:

```python
def test_default_make_drive_storage_central_by_default(tmp_path):
    from mcpbrain import fleet_storage, org_defaults
    factory = onboarding._default_make_drive_storage(str(tmp_path), object())
    fs = factory("D7")
    # central: rooted at the fleet folder, namespaced by source drive id
    assert fs._root == org_defaults.FLEET_FOLDER_ID
    assert fs._base_parts == ["ingest-cache", "D7"]


def test_default_make_drive_storage_none_without_service(tmp_path):
    # No drive service -> a factory that yields None (degrade, don't crash).
    assert onboarding._default_make_drive_storage(str(tmp_path), None)("D7") is None
```

In `tests/test_sync_cycle.py`, add a test that `run_sync_cycle` hands `sync_shared_drives` a central factory:

```python
def test_run_sync_cycle_uses_central_cache_storage(tmp_path, monkeypatch):
    from mcpbrain import config, org_defaults
    from mcpbrain.store import Store
    from mcpbrain.sync import run_sync_cycle
    from mcpbrain.sync import drive as drivemod
    from tests.test_drive_sync import FakeDriveService

    class _Emb:
        dim = 4
        def embed_passages(self, texts):
            return [[1.0, 2.0, 3.0, 4.0] for _ in texts]
        def embed_query(self, text):
            return [0.0, 0.0, 0.0, 0.0]

    home = str(tmp_path / "home")
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}},
        "owner_email": "me@x.org"})
    store = Store(tmp_path / "b.sqlite3", dim=4); store.init()

    captured = {}

    def _spy(service, s, *, pin, storage_factory, absence_threshold=3,
             contextual_retrieval=False):
        captured["fs"] = storage_factory("D1")
        return {"_revoked": []}

    monkeypatch.setattr(drivemod, "sync_shared_drives", _spy)
    # Stub the post-block progressive backfill so the minimal FakeDriveService
    # (no pages/exports seeded) can't trip it — this test only cares which
    # storage the shared-drive block hands to sync_shared_drives.
    import mcpbrain.sync as syncmod
    monkeypatch.setattr(syncmod, "progressive_backfill_step", lambda *a, **k: {})
    svc = FakeDriveService(shared_drives=[{"id": "D1", "name": "Ops"}])
    run_sync_cycle(store, _Emb(), drive_service=svc, home=home)

    assert captured["fs"]._root == org_defaults.FLEET_FOLDER_ID
    assert captured["fs"]._base_parts == ["ingest-cache", "D1"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_onboarding_bootstrap.py -k make_drive_storage tests/test_sync_cycle.py -k central -v`
Expected: FAIL — `_default_make_drive_storage()` takes 1 positional arg (old signature) / the spy captures an in-drive storage (`_root == "D1"`), not the fleet folder.

- [ ] **Step 3: Update `sync/__init__.py`**

Change the import (~line 85) from `drive_cache_storage` to `cache_storage_factory`:

```python
                from mcpbrain.fleet_storage import cache_storage_factory
```

Change the `storage_factory=` argument in the `sync_shared_drives(...)` call (~line 95) from:

```python
                    storage_factory=lambda d: drive_cache_storage(drive_service, d),
```
to:
```python
                    storage_factory=cache_storage_factory(home, drive_service),
```

(Nothing else in the block changes — the backfill step reuses the storages this factory built via `drives_fs`.)

- [ ] **Step 4: Update `onboarding.py`**

Rewrite `_default_make_drive_storage` (~line 70) to take `home` and route through the factory:

```python
def _default_make_drive_storage(home, drive_service):
    """Return a factory building a per-source-drive cache FleetStorage — this is
    what `bootstrap_drive` reads. Central (fleet-folder-rooted) by default via
    cache_storage_factory, or in-drive when ingest_cache_central is off. Degrades
    to a None-returning factory if A is unavailable or there is no drive service."""
    if drive_service is None:
        return lambda drive_id: None
    try:
        from mcpbrain.fleet_storage import cache_storage_factory  # subsystem A
    except ImportError:
        return lambda drive_id: None
    return cache_storage_factory(home, drive_service)
```

Update its caller in `run_bootstrap` (~line 248) to pass `home`:

```python
    if make_drive_storage is None and drive_service is not None:
        make_drive_storage = _default_make_drive_storage(home, drive_service)
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_onboarding_bootstrap.py -k make_drive_storage tests/test_sync_cycle.py -k central -v`
Expected: PASS.

- [ ] **Step 6: Run the impacted suites in full**

Run: `pytest tests/test_sync_cycle.py tests/test_onboarding_bootstrap.py -v`
Expected: PASS — including the pre-existing `test_run_sync_cycle_shared_drive_publishes_after_embed` (it monkeypatches `sync_shared_drives` wholesale, so it is agnostic to the factory swap) and `test_bootstrap_uses_per_drive_storage_not_fleet_folder` (injects `make_drive_storage` directly, bypassing the default).

- [ ] **Step 7: Commit**

```bash
git add mcpbrain/sync/__init__.py mcpbrain/onboarding.py tests/test_sync_cycle.py tests/test_onboarding_bootstrap.py
git commit -m "feat(sync,onboarding): route ingest cache through central factory"
```

---

### Task 4: One-shot legacy-folder cleanup script

Create `bin/relocate_ingest_cache.py` to report and (with `--delete-legacy`) remove the old top-level `.mcpbrain-cache/` folders from each team shared drive.

**Files:**
- Create: `bin/relocate_ingest_cache.py`
- Test: `tests/test_relocate_ingest_cache.py`

**Interfaces:**
- Produces (all take an injected Drive `service` so tests need no network):
  - `scan(service) -> list[dict]` — one `{drive_id, drive_name, folder_id, count}` per drive that has a top-level `.mcpbrain-cache/`.
  - `delete_legacy(service, entries) -> int` — deletes each entry's folder, returns count deleted.
  - `main(argv=None) -> int`.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_relocate_ingest_cache.py`. It reuses `FakeDrive` from the storage test as a Drive double and adds shared-drive enumeration:

```python
import importlib

from tests.test_fleet_storage_drive import FakeDrive
from mcpbrain.fleet_storage import DriveFleetStorage

relocate = importlib.import_module("bin.relocate_ingest_cache")
FOLDER_MIME = "application/vnd.google-apps.folder"


def _seed_in_drive_cache(drive, drive_id, n):
    """Create <drive_id>/.mcpbrain-cache/ with n artifact files (in-drive layout)."""
    fs = DriveFleetStorage(drive, drive_id, root_is_drive=True)
    for i in range(n):
        fs.put_bytes(f".mcpbrain-cache/FID{i}.h.pf.mbc.gz", b"x")


def _patch_drives(monkeypatch, drives):
    monkeypatch.setattr(relocate, "list_shared_drives", lambda svc: drives)


def test_scan_finds_only_drives_with_cache(monkeypatch):
    drive = FakeDrive()
    _seed_in_drive_cache(drive, "D1", 3)
    # D2 has no cache folder
    _patch_drives(monkeypatch, [{"id": "D1", "name": "Ops"}, {"id": "D2", "name": "HR"}])
    entries = relocate.scan(drive)
    assert len(entries) == 1
    assert entries[0]["drive_id"] == "D1"
    assert entries[0]["drive_name"] == "Ops"
    assert entries[0]["count"] == 3


def test_delete_legacy_removes_the_folder(monkeypatch):
    drive = FakeDrive()
    _seed_in_drive_cache(drive, "D1", 2)
    _patch_drives(monkeypatch, [{"id": "D1", "name": "Ops"}])
    entries = relocate.scan(drive)
    deleted = relocate.delete_legacy(drive, entries)
    assert deleted == 1
    # folder is gone -> a re-scan finds nothing
    assert relocate.scan(drive) == []


def test_dry_run_does_not_delete(monkeypatch, capsys):
    drive = FakeDrive()
    _seed_in_drive_cache(drive, "D1", 1)
    _patch_drives(monkeypatch, [{"id": "D1", "name": "Ops"}])
    monkeypatch.setattr(relocate, "_drive_service", lambda home: drive)
    monkeypatch.setattr(relocate.config, "app_dir", lambda: ".")
    rc = relocate.main([])                 # no --delete-legacy
    assert rc == 0
    assert relocate.scan(drive)            # still there
    assert "Dry-run" in capsys.readouterr().out
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_relocate_ingest_cache.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'bin.relocate_ingest_cache'`.

- [ ] **Step 3: Create the script**

Create `bin/relocate_ingest_cache.py`:

```python
"""One-shot cleanup: remove the legacy in-drive `.mcpbrain-cache/` folders left in
each Shared Drive root by the pre-centralization ingest cache.

As of the centralization change, the ingest cache lives under the fleet folder
(<fleet folder>/ingest-cache/<source_drive_id>/.mcpbrain-cache/), so the old
per-team-drive `.mcpbrain-cache/` folders are dead clutter. This removes them.

RUN ONCE, AND ONLY AFTER every install in the fleet has updated to the wheel that
centralizes the cache. An install still on the old code will RECREATE the in-drive
folder on its next sync. Deleting is safe: the central location re-publishes any
still-live document on its next cache-miss (regeneration is cheap; no copy needed).

Usage:
  python bin/relocate_ingest_cache.py                  # dry-run: report only
  python bin/relocate_ingest_cache.py --delete-legacy  # actually delete
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from mcpbrain import config                                    # noqa: E402
from mcpbrain.fleet_storage import list_shared_drives          # noqa: E402
from mcpbrain.ingest_cache import CACHE_DIR                    # noqa: E402

_FOLDER_MIME = "application/vnd.google-apps.folder"


def _drive_service(home):
    from mcpbrain import auth
    creds = auth.load_credentials()
    return auth.build_service("drive", "v3", creds)


def _find_cache_folder(service, drive_id):
    resp = service.files().list(
        q=(f"name = '{CACHE_DIR}' and '{drive_id}' in parents and trashed = false "
           f"and mimeType = '{_FOLDER_MIME}'"),
        corpora="drive", driveId=drive_id,
        includeItemsFromAllDrives=True, supportsAllDrives=True,
        fields="files(id,name)").execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _count_children(service, folder_id):
    n, token = 0, None
    while True:
        resp = service.files().list(
            q=f"'{folder_id}' in parents and trashed = false",
            includeItemsFromAllDrives=True, supportsAllDrives=True,
            fields="nextPageToken, files(id)", pageSize=1000, pageToken=token).execute()
        n += len(resp.get("files", []))
        token = resp.get("nextPageToken")
        if not token:
            break
    return n


def scan(service):
    """Return [{'drive_id','drive_name','folder_id','count'}] for drives that have
    a top-level .mcpbrain-cache/ folder. Per-drive failures are isolated."""
    out = []
    for d in list_shared_drives(service):
        did = d.get("id")
        if not did:
            continue
        name = d.get("name") or "<unnamed>"
        try:
            fid = _find_cache_folder(service, did)
        except Exception as exc:  # noqa: BLE001 — isolate one drive's failure
            print(f"  ! {name} ({did}): scan failed: {exc}")
            continue
        if fid:
            out.append({"drive_id": did, "drive_name": name,
                        "folder_id": fid, "count": _count_children(service, fid)})
    return out


def delete_legacy(service, entries):
    """Delete each entry's .mcpbrain-cache/ folder (and its contents). Per-drive
    isolation; returns the number of folders deleted."""
    deleted = 0
    for e in entries:
        try:
            service.files().delete(
                fileId=e["folder_id"], supportsAllDrives=True).execute()
        except Exception as exc:  # noqa: BLE001 — isolate one drive's failure
            print(f"  ! {e['drive_name']} ({e['drive_id']}): delete failed: {exc}")
            continue
        deleted += 1
        print(f"  ✓ deleted {CACHE_DIR}/ from {e['drive_name']} "
              f"({e['drive_id']}) — {e['count']} artifact(s)")
    return deleted


def main(argv=None):
    ap = argparse.ArgumentParser(prog="relocate_ingest_cache")
    ap.add_argument("--delete-legacy", action="store_true",
                    help="Actually delete (default is a dry-run report).")
    ap.add_argument("--home", default=None)
    ns = ap.parse_args(argv)

    home = ns.home or str(config.app_dir())
    service = _drive_service(home)
    entries = scan(service)
    if not entries:
        print("No legacy in-drive .mcpbrain-cache/ folders found.")
        return 0

    print(f"Found legacy cache in {len(entries)} drive(s):")
    for e in entries:
        print(f"  - {e['drive_name']} ({e['drive_id']}): {e['count']} artifact(s)")

    if not ns.delete_legacy:
        print("\nDry-run. Re-run with --delete-legacy to remove them "
              "(only after the whole fleet has updated).")
        return 0

    n = delete_legacy(service, entries)
    print(f"\nDeleted {n} legacy cache folder(s).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: Ensure `bin/` is importable as a package for the test**

The test does `importlib.import_module("bin.relocate_ingest_cache")`. Check whether `bin/__init__.py` exists:

Run: `ls bin/__init__.py 2>/dev/null || echo MISSING`

If it prints `MISSING`, create an empty `bin/__init__.py`:

```bash
touch bin/__init__.py
```

(If `bin/consolidate.py` is already imported in tests elsewhere via `bin.`, this will already exist — leave it.)

- [ ] **Step 5: Run tests to verify they pass**

Run: `pytest tests/test_relocate_ingest_cache.py -v`
Expected: PASS (3 tests).

- [ ] **Step 6: Verify the dry-run help/CLI parses**

Run: `python bin/relocate_ingest_cache.py --help`
Expected: usage text listing `--delete-legacy` and `--home`; exit 0.

- [ ] **Step 7: Commit**

```bash
git add bin/relocate_ingest_cache.py tests/test_relocate_ingest_cache.py
git commit -m "feat(bin): one-shot legacy in-drive ingest-cache cleanup"
```

---

## Post-implementation (NOT part of the coding tasks)

These are follow-ups Josh drives explicitly — do not do them as part of executing this plan:

1. **Full suite + gold-eval gate**, per `docs/RELEASE-RUNBOOK.md`.
2. **Release** (five version files + the three repos) — a separate, explicitly-authorized action. Add a `docs/RELEASE-RUNBOOK.md` note that after this ships and the fleet has updated, `python bin/relocate_ingest_cache.py --delete-legacy` should be run once.
3. **Escrow-key lockdown** (flagged in the spec's Follow-up): restrict `mcpbrain-fleet/mcpbrain-escrow/` so members can't read other users' recovery keys. Independent of this work.

## Self-Review

**Spec coverage:**
- `base_path` on `DriveFleetStorage` → Task 1. ✓
- Fleet-folder id reuse (no separate driveId resolver) → Task 2 (`fleet_folder_id`). ✓
- `centralized_cache_storage` rooted at fleet folder → Task 2. ✓
- `ingest_cache_central` flag, default True → Task 2. ✓
- Wire `sync/__init__.py` + `onboarding.py` via one `cache_storage_factory` → Task 3. ✓
- `bin/relocate_ingest_cache.py` (dry-run default, `--delete-legacy`, per-drive isolation) → Task 4. ✓
- `ingest_cache.py` untouched → confirmed: no task modifies it; relocation via storage roots only. ✓
- Safe degradation to in-drive → Task 2 `cache_storage_factory` + Task 3 wiring. ✓
- Follow-up (escrow lockdown) → recorded as post-implementation, out of scope. ✓

**Placeholder scan:** No TBD/TODO; every code step shows complete code and exact commands with expected output.

**Type consistency:** `fleet_folder_id(home) -> str | None` defined in Task 2, consumed in Task 2 (`cache_storage_factory`) and reused by Task 3's `_default_make_drive_storage` indirectly through `cache_storage_factory`. `_default_make_drive_storage(home, drive_service)` new signature defined in Task 3 and its single caller updated in the same task. `centralized_cache_storage(drive_service, fleet_folder_id_, source_drive_id)` name/params consistent across Task 2 definition and its factory use. `scan`/`delete_legacy`/`main` consistent across Task 4 script and tests. ✓
