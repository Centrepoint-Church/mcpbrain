# Org Baseline — Phase C (Onboarding / Baseline Bootstrap) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Give a new install a *baseline bootstrap* that runs **before first sync**: import the current org-graph snapshot (instant layer-1 graph) and bulk-import every shared drive's `.mcpbrain-cache/` artifacts (chunks + vectors + enrichment), so normal sync only pays extraction/embedding/enrichment on genuine cache-misses. Build the whole orchestration now, against **fakes** for subsystems A and B, wired through an explicit **dependency-injection seam** so Phase D swaps the fakes for the real A/B functions with no change to C's logic.

**Architecture:** Phase C is the *convergence-bound* track (spec §"Phases A ∥ B ∥ C"): it can be written in full in parallel, but its true end-to-end test is Phase D. So C never imports A's or B's internals. It depends only on:

- **B's interface** — `import_snapshot(store, fleet_storage) -> dict` (from `mcpbrain/org_import.py`, built by B).
- **A's interface** — `bootstrap_drive(store, fleet_storage, drive_id, pin) -> dict` (from `mcpbrain/ingest_cache.py`, built by A).
- **Phase 0's frozen surface** — the `FleetStorage` Protocol, `FleetPin`, `config.fleet_pin()`, `config.org_import_enabled()` / `config.ingest_cache_enabled()`, and the `tests/helpers/org_fleet.py` harness (`LocalDirFleetStorage`, `make_install`, `make_fleet`).

C ships a thin `mcpbrain/onboarding.py`. Its core is the pure orchestrator `bootstrap_baseline(store, fleet_storage, drives, pin, *, import_snapshot=…, bootstrap_drive=…)` — the A/B functions are **injected** (default bindings lazily import the real modules and degrade cleanly while A/B are unbuilt). A glue layer `run_bootstrap(home, store, …)` resolves inputs from config/services, enforces the idempotence marker, and calls the orchestrator. The **real entrypoint** is the daemon: `Daemon.bootstrap_baseline_once()` runs it once before the first `run_cycle` (the `mcpbrain setup` → wizard → first daemon cycle path), and `/api/bootstrap-baseline` + `mcpbrain bootstrap` + a `doctor` step make it re-runnable.

**Why the daemon, not `setup.py`:** `mcpbrain setup` (`mcpbrain/setup.py:207`) only launches the browser wizard; it holds no `Store` and no Google services. The daemon owns the live `Store` and builds Google services via `auth.build_google_services()` (`daemon.py:599 ensure_services`), and "first sync" is `run_cycle` inside `Daemon.run_one` (`daemon.py:1067`). So the baseline-bootstrap step is a **daemon method invoked before that first `run_cycle`**, mirroring how `start_enrich_backfill` (`daemon.py:955`) and `/api/backup/auto` (`control_api.py:437`) already bootstrap state right after Google sign-in.

**Tech Stack:** Python 3, stdlib `json`/`pathlib`/`datetime`, pytest. No new dependencies.

## Global Constraints

- **The injection seam is the whole point.** `bootstrap_baseline` takes `import_snapshot` and `bootstrap_drive` as keyword args with default bindings. Tests inject fakes; prod uses the defaults. Nothing in C imports `mcpbrain.org_import` or `mcpbrain.ingest_cache` at module top level — the default bindings import them **lazily inside the call** and degrade to a benign `{"status": "unavailable"}` on `ImportError`, so `mcpbrain/onboarding.py` imports cleanly today (before A/B exist) and activates automatically once they land.
- **Do NOT modify the Phase 0 shared surface.** No schema changes (no new column/table), no new `config.py` flag accessor or change to `fleet_pin`, no change to the frozen cadence registration slots in `daemon.py`. C adds only *its own* logic (a new module, a daemon method + a first-cycle call, an endpoint, a client method, a CLI verb, a doctor line).
- **Do NOT implement A's or B's internals.** Only call their interfaces (`import_snapshot`, `bootstrap_drive`, and A's `DriveFleetStorage` / shared-drive enumeration via the default factories). In this plan they are faked.
- **Idempotence marker is a plain file**, `<home>/baseline_bootstrap.json` — deliberately NOT config and NOT schema, so it touches no frozen surface. It records `snapshot_done`, the set of completed `done_drive_ids` (for resume), and `completed_at`.
- **Degrade, never crash.** No fleet folder / no snapshot / no pin / daemon down / one bad drive → skip that piece cleanly and let normal sync handle the rest. The daemon call is wrapped so bootstrap can never break a sync cycle.
- **Ordering is a contract:** snapshot import is attempted **before** any drive cache import (a new user sees the graph skeleton first). Per-drive imports are independent — one drive erroring never blocks the others.
- **Tests:** pytest, flat `tests/test_*.py`, functions `test_*`. Construct stores as `Store(tmp_path / "x.sqlite3", dim=4)` then `.init()`. Use `tests/helpers/org_fleet.py` (`LocalDirFleetStorage`, `make_install`, `make_fleet`). Fakes for `import_snapshot` / `bootstrap_drive` are simple in-test closures that record calls and mutate the store, proving orchestration/ordering/idempotence/degradation.
- **No version bump, no release, no push** (per `CLAUDE.md`: shipping is a separate explicit instruction). Commit locally only.
- Reference spec: `docs/superpowers/specs/2026-07-03-org-baseline-personal-overlay-design.md` (subsystem C, and the phasing note that C is convergence-bound).

---

## File Structure

**Created:**
- `mcpbrain/onboarding.py` — the whole subsystem C surface: the pure orchestrator `bootstrap_baseline`, the injectable default bindings + degradable input factories (the convergence seam), the marker helpers, `should_bootstrap`, the `run_bootstrap` glue, and the `bootstrap_main` CLI entry. One responsibility: *orchestrate the pre-first-sync baseline import, testable with fakes now*.
- `tests/test_onboarding_bootstrap.py` — orchestrator ordering/degradation/resume (Task 1) + convergence-seam degradation (Task 2).
- `tests/test_onboarding_run.py` — marker/idempotence/flags glue (Task 3) + end-to-end LocalDirFleetStorage gate (Task 7).
- `tests/test_onboarding_daemon.py` — daemon `bootstrap_baseline_once` (Task 4).
- `tests/test_onboarding_api.py` — `/api/bootstrap-baseline` + control client (Task 5).
- `tests/test_onboarding_doctor_cli.py` — doctor re-run + `mcpbrain bootstrap` (Task 6).

**Modified:**
- `mcpbrain/daemon.py` — `__init__` flag `self._baseline_bootstrap_done` (~daemon.py:587), a call in `run_one` after `ensure_services()` (~daemon.py:1041), and the `bootstrap_baseline_once` method (next to `start_enrich_backfill`, ~daemon.py:955).
- `mcpbrain/control_api.py` — `/api/bootstrap-baseline` POST route (after the enrich-backfill routes, ~control_api.py:346).
- `mcpbrain/control_client.py` — `bootstrap_baseline()` method (after `cancel_enrich_backfill`, ~control_client.py:85).
- `mcpbrain/cli.py` — register the `bootstrap` subcommand (name tuple ~cli.py:21, dispatch dict ~cli.py:30).
- `mcpbrain/doctor.py` — a `baseline` repair in `_default_repairs` (~doctor.py:96) and a "Baseline" report block in `run_doctor` (after the embedder block, ~doctor.py:238).

---

## Task 1: Orchestrator `bootstrap_baseline` — ordering, degradation, resume

**Files:**
- Create: `mcpbrain/onboarding.py`
- Test: `tests/test_onboarding_bootstrap.py`

**Interfaces:**
- Consumes (by contract, faked here): `import_snapshot(store, fleet_storage) -> dict` (B), `bootstrap_drive(store, fleet_storage, drive_id, pin) -> dict` (A), `FleetPin` (Phase 0), a `FleetStorage` (Phase 0).
- Produces: `bootstrap_baseline(store, fleet_storage, drives, pin, *, import_snapshot=…, bootstrap_drive=…, done_drive_ids=(), snapshot_done=False) -> dict`. The dict shape (frozen for the glue + tests):
  ```python
  {"snapshot": {"status": "imported"|"unchanged"|"no_snapshot"|"skipped"|"error"|"unavailable", "detail": ...},
   "drives": {drive_id: {"status": "ok"|"skipped"|"error"|"unavailable", "detail"/"reason": ...}},
   "snapshot_done": bool,          # True once a snapshot was imported (or already was)
   "done_drive_ids": set[str],     # union of prior + this run's successful drives (resume)
   "cache_hits": int,              # summed across successful drives
   "errors": [str, ...]}
  ```
- **Contract C relies on from A/B returns:** `import_snapshot` returns a dict; `status in {"imported","unchanged"}` ⇒ success, `"no_snapshot"` ⇒ benign (nothing published yet), anything else/raise ⇒ error. `bootstrap_drive` returns a dict with an optional integer `"cache_hits"`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_bootstrap.py`:

```python
from mcpbrain import onboarding
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    return s


def _pin(pinned=True):
    return FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
                    fleet_secret="s3cret" if pinned else "")


def _fakes(calls, *, snap_status="imported", drive_hits=2, raise_on=()):
    """Return (import_snapshot, bootstrap_drive) fakes that record call order
    and mutate the store, so a test can prove real work happened + ordering."""
    def import_snapshot(store, fleet_storage):
        calls.append("snapshot")
        with store._connect() as db:
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                       "VALUES('ceo','CEO','person','org')")
        return {"status": snap_status, "entity_count": 1}

    def bootstrap_drive(store, fleet_storage, drive_id, pin):
        calls.append(f"drive:{drive_id}")
        if drive_id in raise_on:
            raise RuntimeError(f"boom on {drive_id}")
        with store._connect() as db:
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                       f"VALUES('doc-{drive_id}','Doc','document','local')")
        return {"cache_hits": drive_hits, "drive_id": drive_id}

    return import_snapshot, bootstrap_drive


def test_snapshot_imported_before_any_drive(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1", "D2"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot)
    # Ordering contract: snapshot first, then drives in order.
    assert calls == ["snapshot", "drive:D1", "drive:D2"]
    assert res["snapshot"]["status"] == "imported"
    assert res["snapshot_done"] is True
    assert res["drives"]["D1"]["status"] == "ok"
    assert res["cache_hits"] == 4
    assert res["done_drive_ids"] == {"D1", "D2"}


def test_no_fleet_storage_skips_everything(tmp_path):
    store = _store(tmp_path)
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, None, ["D1"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot)
    assert calls == []                                   # nothing called
    assert res["snapshot"]["status"] == "skipped"
    assert res["snapshot"]["reason"] == "no_fleet_storage"
    assert res["drives"] == {}
    assert res["done_drive_ids"] == set()


def test_unpinned_skips_drive_cache_but_imports_snapshot(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1"], _pin(pinned=False),
        import_snapshot=imp, bootstrap_drive=boot)
    assert calls == ["snapshot"]                         # drive cache skipped
    assert res["snapshot_done"] is True
    assert res["drives"]["D1"]["status"] == "skipped"
    assert res["drives"]["D1"]["reason"] == "no_pin"


def test_no_snapshot_is_benign_and_drives_still_run(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls, snap_status="no_snapshot")
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["snapshot"]["status"] == "no_snapshot"
    assert res["snapshot_done"] is False                 # nothing to import
    assert res["drives"]["D1"]["status"] == "ok"


def test_one_bad_drive_does_not_block_the_rest(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls, raise_on=("D1",))
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1", "D2"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["drives"]["D1"]["status"] == "error"
    assert res["drives"]["D2"]["status"] == "ok"
    assert res["done_drive_ids"] == {"D2"}               # errored drive not marked done
    assert any("D1" in e for e in res["errors"])


def test_snapshot_error_is_caught_and_drives_still_run(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []

    def imp(store, fs_):
        calls.append("snapshot")
        raise RuntimeError("corrupt manifest")

    _, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1"], _pin(), import_snapshot=imp, bootstrap_drive=boot)
    assert res["snapshot"]["status"] == "error"
    assert res["snapshot_done"] is False
    assert res["drives"]["D1"]["status"] == "ok"         # degraded, not aborted


def test_resume_skips_already_done_drives_and_snapshot(tmp_path):
    store = _store(tmp_path)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.bootstrap_baseline(
        store, fs, ["D1", "D2"], _pin(),
        import_snapshot=imp, bootstrap_drive=boot,
        done_drive_ids={"D1"}, snapshot_done=True)
    assert calls == ["drive:D2"]                          # snapshot + D1 skipped
    assert res["snapshot"]["status"] == "skipped"
    assert res["drives"]["D1"]["status"] == "skipped"
    assert res["drives"]["D2"]["status"] == "ok"
    assert res["done_drive_ids"] == {"D1", "D2"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_onboarding_bootstrap.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'mcpbrain.onboarding'`.

- [ ] **Step 3: Write minimal implementation**

Create `mcpbrain/onboarding.py` (Task 1 introduces the orchestrator + the default-binding *names* it references; the default bindings' bodies land fully in Task 2 — here they are minimal so import works):

```python
"""Subsystem C — onboarding / baseline bootstrap.

Runs once before an install's first sync: import the org-graph snapshot
(subsystem B) for an instant layer-1 graph, then bulk-import each shared drive's
.mcpbrain-cache/ artifacts (subsystem A). Normal sync then only pays extraction
on genuine cache-misses.

Convergence-bound (spec §"Phases A ∥ B ∥ C"): C never imports A's or B's
internals. It depends only on their *interfaces*, injected into
``bootstrap_baseline``:

    import_snapshot(store, fleet_storage) -> dict            # B, org_import.py
    bootstrap_drive(store, fleet_storage, drive_id, pin)->dict  # A, ingest_cache.py

Tests inject fakes; prod uses the default bindings below, which lazily import
the real modules and degrade to {"status": "unavailable"} until A/B land. This
module therefore imports cleanly today and activates automatically at Phase D.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)


# -- injectable A/B bindings (bodies completed in Task 2) -------------------

def _default_import_snapshot(store, fleet_storage) -> dict:
    try:
        from mcpbrain.org_import import import_snapshot as _real  # subsystem B
    except ImportError:
        return {"status": "unavailable", "reason": "org_import not built (Phase B)"}
    return _real(store, fleet_storage)


def _default_bootstrap_drive(store, fleet_storage, drive_id, pin) -> dict:
    try:
        from mcpbrain.ingest_cache import bootstrap_drive as _real  # subsystem A
    except ImportError:
        return {"status": "unavailable", "cache_hits": 0,
                "reason": "ingest_cache not built (Phase A)"}
    return _real(store, fleet_storage, drive_id, pin)


# -- orchestrator (pure; no config, no I/O beyond the injected callables) ---

_SNAPSHOT_OK = {"imported", "unchanged"}


def bootstrap_baseline(store, fleet_storage, drives, pin, *,
                       import_snapshot=_default_import_snapshot,
                       bootstrap_drive=_default_bootstrap_drive,
                       done_drive_ids=(), snapshot_done=False) -> dict:
    """Import the org snapshot then each drive's ingest cache. Ordering is a
    contract: snapshot first. Degrades cleanly (no transport / no pin / no
    snapshot / a bad drive) and resumes (done_drive_ids / snapshot_done)."""
    done = set(done_drive_ids)
    errors: list[str] = []
    result = {"snapshot": {}, "drives": {}, "snapshot_done": bool(snapshot_done),
              "done_drive_ids": done, "cache_hits": 0, "errors": errors}

    if fleet_storage is None:
        result["snapshot"] = {"status": "skipped", "reason": "no_fleet_storage"}
        return result

    # 1) snapshot (layer-1 graph skeleton) — always before any drive cache.
    if snapshot_done:
        result["snapshot"] = {"status": "skipped", "reason": "already_imported"}
    else:
        try:
            detail = import_snapshot(store, fleet_storage) or {}
            status = detail.get("status", "imported")
            result["snapshot"] = {"status": status, "detail": detail}
            if status in _SNAPSHOT_OK:
                result["snapshot_done"] = True
        except Exception as exc:  # noqa: BLE001 — degrade, keep going to drives
            log.warning("baseline snapshot import failed: %s", exc, exc_info=True)
            result["snapshot"] = {"status": "error", "detail": str(exc)}
            errors.append(f"snapshot: {exc}")

    # 2) per-drive ingest cache — needs the fleet pin (embed_model/dim must match
    #    so imported artifacts are usable). No pin => skip; normal sync handles it.
    for drive_id in drives:
        if drive_id in done:
            result["drives"][drive_id] = {"status": "skipped", "reason": "already_done"}
            continue
        if not pin.is_pinned:
            result["drives"][drive_id] = {"status": "skipped", "reason": "no_pin"}
            continue
        try:
            detail = bootstrap_drive(store, fleet_storage, drive_id, pin) or {}
            result["drives"][drive_id] = {"status": "ok", "detail": detail}
            result["cache_hits"] += int(detail.get("cache_hits", 0) or 0)
            done.add(drive_id)
        except Exception as exc:  # noqa: BLE001 — one bad drive never blocks the rest
            log.warning("baseline cache import failed for %s: %s", drive_id, exc,
                        exc_info=True)
            result["drives"][drive_id] = {"status": "error", "detail": str(exc)}
            errors.append(f"drive {drive_id}: {exc}")

    return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_onboarding_bootstrap.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/onboarding.py tests/test_onboarding_bootstrap.py
git commit -m "feat(onboarding): baseline-bootstrap orchestrator (snapshot-then-cache, DI, degrade/resume)"
```

---

## Task 2: Convergence seam — default bindings + input factories degrade cleanly

**Files:**
- Modify: `mcpbrain/onboarding.py` (add `_default_make_fleet_storage`, `_default_enumerate_drives`, `_fleet_folder_id`)
- Test: `tests/test_onboarding_bootstrap.py` (extend)

**Interfaces:**
- Consumes: `config.read_config`, `org_defaults.FLEET_FOLDER_ID` (Phase 0), and — **only in prod** — A's `mcpbrain.ingest_cache.DriveFleetStorage` + shared-drive enumeration.
- Produces: the two input factories the glue (Task 3) uses to resolve a real `FleetStorage` and the drive list, each degrading to `None` / `[]` when A is unbuilt or inputs are missing. These four functions (`_default_import_snapshot`, `_default_bootstrap_drive`, `_default_make_fleet_storage`, `_default_enumerate_drives`) are **exactly the seams Phase D reconciles with A/B's real symbols** — see Self-Review.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_onboarding_bootstrap.py`:

```python
def test_default_bindings_degrade_when_A_B_unbuilt():
    # Until subsystems A/B land, the default bindings must not raise — they
    # report "unavailable" so the orchestrator no-ops safely in prod today.
    assert onboarding._default_import_snapshot(object(), object())["status"] == "unavailable"
    d = onboarding._default_bootstrap_drive(object(), object(), "D1", _pin())
    assert d["status"] == "unavailable" and d["cache_hits"] == 0


def test_default_make_fleet_storage_none_without_service(tmp_path):
    assert onboarding._default_make_fleet_storage(str(tmp_path), None) is None


def test_default_enumerate_drives_empty_without_service():
    assert onboarding._default_enumerate_drives(None) == []


def test_default_factories_degrade_when_A_unbuilt(tmp_path):
    # A drive_service is present but subsystem A's DriveFleetStorage/enumeration
    # module isn't built yet -> factories degrade rather than crash onboarding.
    assert onboarding._default_make_fleet_storage(str(tmp_path), object()) is None
    assert onboarding._default_enumerate_drives(object()) == []


def test_fleet_folder_id_falls_back_to_org_default(tmp_path):
    from mcpbrain import org_defaults
    assert onboarding._fleet_folder_id(str(tmp_path)) == org_defaults.FLEET_FOLDER_ID
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_onboarding_bootstrap.py -k "default_ or fleet_folder" -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.onboarding' has no attribute '_default_make_fleet_storage'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/onboarding.py`, add after `_default_bootstrap_drive`:

```python
def _fleet_folder_id(home) -> str:
    """The fleet folder id: local config's fleet.folder_id, else the baked-in
    org default (org_defaults.FLEET_FOLDER_ID)."""
    from mcpbrain import config, org_defaults
    fleet = config.read_config(home).get("fleet") or {}
    return fleet.get("folder_id") or org_defaults.FLEET_FOLDER_ID


def _default_make_fleet_storage(home, drive_service):
    """Build the prod FleetStorage over Google Drive (subsystem A's
    DriveFleetStorage). Degrades to None when there is no drive service, no
    fleet folder, or A is not built yet. Phase D wires the real symbol."""
    if drive_service is None:
        return None
    try:
        from mcpbrain.ingest_cache import DriveFleetStorage  # subsystem A
    except ImportError:
        return None
    folder_id = _fleet_folder_id(home)
    if not folder_id:
        return None
    return DriveFleetStorage(drive_service, folder_id)


def _default_enumerate_drives(drive_service) -> list[str]:
    """Enumerate accessible shared-drive ids (subsystem A). Degrades to [] with
    no service or before A is built. Phase D wires the real enumeration."""
    if drive_service is None:
        return []
    try:
        from mcpbrain.ingest_cache import list_shared_drive_ids  # subsystem A
    except ImportError:
        return []
    return list(list_shared_drive_ids(drive_service))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_onboarding_bootstrap.py -v`
Expected: PASS (all tests).

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/onboarding.py tests/test_onboarding_bootstrap.py
git commit -m "feat(onboarding): convergence-seam default bindings + degradable input factories"
```

---

## Task 3: Marker + `should_bootstrap` + `run_bootstrap` glue (flags, pin, idempotence)

**Files:**
- Modify: `mcpbrain/onboarding.py` (add marker helpers, `should_bootstrap`, `run_bootstrap`)
- Test: `tests/test_onboarding_run.py`

**Interfaces:**
- Consumes: `config.org_import_enabled`, `config.ingest_cache_enabled`, `config.fleet_pin`, `config.is_configured` (Phase 0 + existing), `bootstrap_baseline` (Task 1), the factories (Task 2).
- Produces:
  - `should_bootstrap(home) -> bool` — cheap per-cycle gate for the daemon: `False` if already completed (marker `completed_at`), if not `is_configured`, or if both org flags are off.
  - `run_bootstrap(home, store, *, drive_service=None, fleet_storage=None, drives=None, pin=None, force=False, import_snapshot=…, bootstrap_drive=…, make_fleet_storage=…, enumerate_drives=…) -> dict` — resolves inputs from config/services, loads the resume marker, calls `bootstrap_baseline`, persists the marker, returns the summary tagged `status` in `{"skipped","degraded","done"}`. `"done"` writes `completed_at` (finalized); `"degraded"` (no transport yet) persists resume state but leaves `completed_at` empty so the daemon retries; `"skipped"` (flags off / already done) writes nothing new.

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_run.py`:

```python
import json

from mcpbrain import onboarding
from mcpbrain.org_contracts import FleetPin
from tests.helpers.org_fleet import make_install


def _configure(home, *, import_on=True, cache_on=True, pinned=True):
    from mcpbrain import config
    cfg = {"owner_name": "Al", "owner_email": "al@x.org",
           "orgs": [{"name": "Acme"}],
           "org_import_enabled": import_on, "ingest_cache": cache_on}
    if pinned:
        cfg["org_config"] = {"org_pin": {"embed_model": "bge-small", "dim": 4,
                                         "chunker_version": "v1",
                                         "fleet_secret": "s3cret"}}
    config.write_config(str(home), cfg)


def _fakes(calls):
    def imp(store, fs):
        calls.append("snapshot")
        return {"status": "imported", "entity_count": 1}

    def boot(store, fs, drive_id, pin):
        calls.append(f"drive:{drive_id}")
        return {"cache_hits": 3, "drive_id": drive_id}
    return imp, boot


def test_flags_off_skips(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home, import_on=False, cache_on=False)
    res = onboarding.run_bootstrap(str(inst.home), inst.store)
    assert res["status"] == "skipped" and res["reason"] == "flags_off"


def test_done_writes_marker_and_second_run_is_skipped(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home)
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["status"] == "done"
    marker = json.loads((inst.home / "baseline_bootstrap.json").read_text())
    assert marker["snapshot_done"] is True
    assert marker["done_drive_ids"] == ["D1"]
    assert marker["completed_at"]
    # Second run: marker present -> skipped, no fakes re-invoked.
    calls.clear()
    res2 = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res2["status"] == "skipped" and res2["reason"] == "already_bootstrapped"
    assert calls == []


def test_force_reruns_even_when_marked_done(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home)
    (inst.home / "baseline_bootstrap.json").write_text(
        json.dumps({"snapshot_done": True, "done_drive_ids": ["D1"],
                    "completed_at": "2026-01-01T00:00:00Z"}))
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D2"], force=True,
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["status"] == "done"
    # resume: prior snapshot_done + D1 preserved, D2 newly imported.
    assert calls == ["drive:D2"]
    marker = json.loads((inst.home / "baseline_bootstrap.json").read_text())
    assert set(marker["done_drive_ids"]) == {"D1", "D2"}


def test_no_transport_is_degraded_and_retryable(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home)
    calls = []
    imp, boot = _fakes(calls)
    # make_fleet_storage returns None -> degraded, marker NOT finalized.
    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, drive_service=object(),
        make_fleet_storage=lambda h, s: None,
        enumerate_drives=lambda s: ["D1"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["status"] == "degraded"
    marker = json.loads((inst.home / "baseline_bootstrap.json").read_text())
    assert marker["completed_at"] == ""          # so the daemon retries next cycle
    assert onboarding.should_bootstrap(str(inst.home)) is True


def test_pin_resolved_from_config_gates_cache(tmp_path):
    inst = make_install(tmp_path, "al")
    _configure(inst.home, pinned=False)          # no fleet_secret -> not pinned
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    calls = []
    imp, boot = _fakes(calls)
    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res["status"] == "done"
    assert calls == ["snapshot"]                 # unpinned -> drive cache skipped
    assert res["drives"]["D1"]["reason"] == "no_pin"


def test_should_bootstrap_gate(tmp_path):
    inst = make_install(tmp_path, "al")
    # Not configured yet -> False.
    assert onboarding.should_bootstrap(str(inst.home)) is False
    _configure(inst.home)
    assert onboarding.should_bootstrap(str(inst.home)) is True
    # Completed marker -> False.
    (inst.home / "baseline_bootstrap.json").write_text(
        json.dumps({"completed_at": "2026-01-01T00:00:00Z"}))
    assert onboarding.should_bootstrap(str(inst.home)) is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_onboarding_run.py -v`
Expected: FAIL — `AttributeError: module 'mcpbrain.onboarding' has no attribute 'run_bootstrap'`.

- [ ] **Step 3: Write minimal implementation**

In `mcpbrain/onboarding.py`, add after the orchestrator:

```python
# -- marker (idempotence + resume; a plain file, NOT config/schema) ---------

def _marker_path(home) -> Path:
    return Path(home) / "baseline_bootstrap.json"


def _read_marker(home) -> dict:
    p = _marker_path(home)
    if not p.is_file():
        return {}
    try:
        data = json.loads(p.read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_marker(home, data) -> None:
    try:
        _marker_path(home).write_text(json.dumps(data, indent=2))
    except OSError as exc:
        log.warning("could not write baseline marker: %s", exc)


def _utcnow_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# -- gate + glue ------------------------------------------------------------

def should_bootstrap(home) -> bool:
    """Cheap per-cycle gate for the daemon: worth attempting only once the
    install is configured, at least one org flag is on, and it isn't done."""
    from mcpbrain import config
    if _read_marker(home).get("completed_at"):
        return False
    if not config.is_configured(home):
        return False
    return config.org_import_enabled(home) or config.ingest_cache_enabled(home)


def run_bootstrap(home, store, *, drive_service=None, fleet_storage=None,
                  drives=None, pin=None, force=False,
                  import_snapshot=_default_import_snapshot,
                  bootstrap_drive=_default_bootstrap_drive,
                  make_fleet_storage=_default_make_fleet_storage,
                  enumerate_drives=_default_enumerate_drives) -> dict:
    """Resolve inputs from config/services, enforce the idempotence marker, run
    the orchestrator, persist resume state. Returns the summary tagged with a
    top-level 'status' in {'skipped','degraded','done'}."""
    from mcpbrain import config
    import_on = config.org_import_enabled(home)
    cache_on = config.ingest_cache_enabled(home)
    if not force and not (import_on or cache_on):
        return {"status": "skipped", "reason": "flags_off"}

    marker = _read_marker(home)
    if not force and marker.get("completed_at"):
        return {"status": "skipped", "reason": "already_bootstrapped",
                "completed_at": marker["completed_at"]}

    if pin is None:
        pin = config.fleet_pin(home)
    if fleet_storage is None:
        fleet_storage = make_fleet_storage(home, drive_service)
    if drives is None:
        drives = enumerate_drives(drive_service) if cache_on else []

    prev_done = set(marker.get("done_drive_ids") or [])
    prev_snapshot = bool(marker.get("snapshot_done"))
    summary = bootstrap_baseline(
        store, fleet_storage, list(drives), pin,
        import_snapshot=import_snapshot, bootstrap_drive=bootstrap_drive,
        done_drive_ids=prev_done,
        # If import is off, treat the snapshot as already handled (skip it).
        snapshot_done=prev_snapshot or not import_on)

    degraded = fleet_storage is None
    _write_marker(home, {
        "snapshot_done": summary["snapshot_done"],
        "done_drive_ids": sorted(summary["done_drive_ids"]),
        "cache_hits": summary.get("cache_hits", 0),
        # Finalize only when a real transport existed; a degraded run (no fleet
        # folder yet) stays retryable so a later cycle completes it.
        "completed_at": "" if degraded else _utcnow_iso(),
    })
    summary["status"] = "degraded" if degraded else "done"
    # done_drive_ids is a set (from the orchestrator) — make it JSON-friendly for
    # any caller that serializes the summary (control API).
    summary["done_drive_ids"] = sorted(summary["done_drive_ids"])
    return summary
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_onboarding_run.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/onboarding.py tests/test_onboarding_run.py
git commit -m "feat(onboarding): run_bootstrap glue + idempotence marker + should_bootstrap gate"
```

---

## Task 4: Daemon hook — `bootstrap_baseline_once` before the first sync

**Files:**
- Modify: `mcpbrain/daemon.py` (`__init__` flag ~587, `run_one` call ~1041, new method ~955)
- Test: `tests/test_onboarding_daemon.py`

**Interfaces:**
- Consumes: `onboarding.should_bootstrap`, `onboarding.run_bootstrap` (Task 3); `self._store`, `self.ensure_services()`, `config.app_dir`.
- Produces: `Daemon.bootstrap_baseline_once(services=None, *, force=False) -> dict | None` — runs the baseline import once (in-process flag + on-disk marker), never raises into the cycle, and is invoked at the top of `run_one` before `run_cycle`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_daemon.py`:

```python
from mcpbrain import daemon as d
from mcpbrain.daemon import Daemon
from mcpbrain.store import Store


class _Emb:
    def embed_query(self, q): return [0.0, 0.0, 0.0, 0.0]
    def embed_documents(self, xs): return [[0.0] * 4 for _ in xs]


def _daemon(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4); s.init()
    return Daemon(s, _Emb(), services={"drive_service": object()})


def test_bootstrap_runs_once_then_noops(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(d.onboarding, "should_bootstrap", lambda home: True)
    calls = []
    monkeypatch.setattr(d.onboarding, "run_bootstrap",
                        lambda home, store, **kw: calls.append(kw) or {"status": "done"})
    dm = _daemon(tmp_path)
    assert dm.bootstrap_baseline_once() == {"status": "done"}
    assert dm.bootstrap_baseline_once() is None      # in-proc flag -> no-op
    assert len(calls) == 1
    # the drive_service from services is forwarded to run_bootstrap
    assert "drive_service" in calls[0]


def test_degraded_does_not_set_done_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(d.onboarding, "should_bootstrap", lambda home: True)
    n = {"i": 0}

    def _run(home, store, **kw):
        n["i"] += 1
        return {"status": "degraded"}
    monkeypatch.setattr(d.onboarding, "run_bootstrap", _run)
    dm = _daemon(tmp_path)
    dm.bootstrap_baseline_once()
    dm.bootstrap_baseline_once()                     # degraded -> retried
    assert n["i"] == 2


def test_gate_skips_when_should_bootstrap_false(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(d.onboarding, "should_bootstrap", lambda home: False)
    called = []
    monkeypatch.setattr(d.onboarding, "run_bootstrap",
                        lambda *a, **k: called.append(1) or {"status": "done"})
    dm = _daemon(tmp_path)
    assert dm.bootstrap_baseline_once() is None
    assert called == []


def test_never_raises_into_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(d.onboarding, "should_bootstrap", lambda home: True)

    def _boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(d.onboarding, "run_bootstrap", _boom)
    dm = _daemon(tmp_path)
    res = dm.bootstrap_baseline_once()               # must not raise
    assert res["status"] == "error"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_onboarding_daemon.py -v`
Expected: FAIL — `AttributeError: 'Daemon' object has no attribute 'bootstrap_baseline_once'` (and `module 'mcpbrain.daemon' has no attribute 'onboarding'`).

- [ ] **Step 3: Write minimal implementation**

**(3a)** In `daemon.py`, add the import near the top-level imports (after `daemon.py:48 from mcpbrain.config import app_dir`), so `d.onboarding` resolves and monkeypatch works:

```python
from mcpbrain import onboarding
```

**(3b)** In `Daemon.__init__`, alongside the backfill guards (after `self._backfill_lock = threading.Lock()`, ~daemon.py:587), add:

```python
        # Baseline bootstrap (subsystem C): import the org snapshot + shared-drive
        # ingest caches once, before the first sync. In-process latch; the on-disk
        # marker (onboarding.run_bootstrap) makes it idempotent across restarts.
        self._baseline_bootstrap_done = False
```

**(3c)** In `run_one`, immediately after `services = self.ensure_services()` (daemon.py:1041), add:

```python
        # Before the first real sync: seed the graph from the org snapshot and
        # bulk-import shared-drive caches, so run_cycle only extracts cache-misses.
        self.bootstrap_baseline_once(services)
```

**(3d)** Add the method next to `start_enrich_backfill` (~daemon.py:955):

```python
    def bootstrap_baseline_once(self, services=None, *, force=False) -> dict | None:
        """Import the org snapshot + shared-drive ingest caches before first sync.

        Idempotent: a no-op after it completes once (in-process latch + on-disk
        marker); re-runnable with force=True (doctor / `mcpbrain bootstrap`).
        Degrades cleanly (no fleet folder / snapshot / pin) and never raises into
        the sync cycle."""
        home = str(app_dir())
        if not force and self._baseline_bootstrap_done:
            return None
        if not force and not onboarding.should_bootstrap(home):
            return None
        if services is None:
            services = self.ensure_services()
        try:
            result = onboarding.run_bootstrap(
                home, self._store,
                drive_service=services.get("drive_service"), force=force)
        except Exception as exc:  # noqa: BLE001 — bootstrap must never break sync
            log.warning("baseline bootstrap failed: %s", exc, exc_info=True)
            return {"status": "error", "error": str(exc)}
        if result.get("status") == "done":
            self._baseline_bootstrap_done = True
        return result
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_onboarding_daemon.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/daemon.py tests/test_onboarding_daemon.py
git commit -m "feat(daemon): run baseline bootstrap once before the first sync cycle"
```

---

## Task 5: Control endpoint `/api/bootstrap-baseline` + client method

**Files:**
- Modify: `mcpbrain/control_api.py` (POST route after the enrich-backfill routes, ~control_api.py:346)
- Modify: `mcpbrain/control_client.py` (method after `cancel_enrich_backfill`, ~control_client.py:85)
- Test: `tests/test_onboarding_api.py`

**Interfaces:**
- Consumes: `Daemon.bootstrap_baseline_once` (Task 4).
- Produces: `POST /api/bootstrap-baseline` → runs `d.bootstrap_baseline_once(force=True)` synchronously (like `/api/backup/auto`) and returns the summary as JSON; `ControlClient.bootstrap_baseline()` → POSTs it with a long timeout (bootstrap can download).

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_api.py`:

```python
import json
import urllib.error
import urllib.request

from mcpbrain.control_api import ControlServer
from mcpbrain.store import Store


class FakeDaemon:
    def __init__(self): self.calls = []
    def status(self): return {"google_connected": False, "granted_scopes": []}
    def bootstrap_baseline_once(self, services=None, *, force=False):
        self.calls.append(force)
        return {"status": "done", "cache_hits": 5, "done_drive_ids": ["D1"]}


def _post(port, token, path):
    data = b"{}"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}{path}", data=data,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json",
                 "Content-Length": str(len(data)), "Host": "127.0.0.1"},
        method="POST")
    try:
        with urllib.request.urlopen(req) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_bootstrap_endpoint_runs_forced(tmp_path):
    store = Store(tmp_path / "brain.sqlite3", dim=4); store.init()
    dm = FakeDaemon()
    srv = ControlServer(dm, str(tmp_path), store=store)
    srv.start()
    try:
        status, body = _post(srv.port, srv.token, "/api/bootstrap-baseline")
        assert status == 200
        assert body["status"] == "done" and body["cache_hits"] == 5
        assert dm.calls == [True]           # forced
    finally:
        srv.stop()


def test_control_client_calls_endpoint(tmp_path, monkeypatch):
    from mcpbrain import control_client
    captured = {}

    def _fake_request(self, path, method="GET"):
        captured["path"] = path
        captured["method"] = method
        return {"status": "done"}
    monkeypatch.setattr(control_client.ControlClient, "_request", _fake_request)
    cc = control_client.ControlClient(str(tmp_path))
    assert cc.bootstrap_baseline() == {"status": "done"}
    assert captured == {"path": "/api/bootstrap-baseline", "method": "POST"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_onboarding_api.py -v`
Expected: FAIL — endpoint returns 404 (`test_bootstrap_endpoint_runs_forced`); `AttributeError: 'ControlClient' object has no attribute 'bootstrap_baseline'`.

- [ ] **Step 3: Write minimal implementation**

**(3a)** In `mcpbrain/control_api.py`, in `_handle_post` after the enrich-backfill routes (after `control_api.py:346`), add:

```python
            if h.path == "/api/bootstrap-baseline":
                # Import the org snapshot + shared-drive ingest caches before the
                # first sync, or re-run on demand (wizard / doctor / `mcpbrain
                # bootstrap`). Synchronous like /api/backup/auto; returns the
                # summary. force=True because an explicit call means "run it now".
                return h_json(h, 200,
                              d.bootstrap_baseline_once(force=True) or {"status": "skipped"})
```

**(3b)** In `mcpbrain/control_client.py`, after `cancel_enrich_backfill` (control_client.py:85), add:

```python
    def bootstrap_baseline(self) -> dict:
        """POST /api/bootstrap-baseline — import the org snapshot + shared-drive
        ingest caches (re-runnable; idempotent daemon-side)."""
        return self._request("/api/bootstrap-baseline", method="POST")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_onboarding_api.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/control_api.py mcpbrain/control_client.py tests/test_onboarding_api.py
git commit -m "feat(control): /api/bootstrap-baseline endpoint + client method"
```

---

## Task 6: `doctor` re-run step + `mcpbrain bootstrap` CLI

**Files:**
- Modify: `mcpbrain/doctor.py` (`_default_repairs` ~doctor.py:96; report block after the embedder block ~doctor.py:238)
- Modify: `mcpbrain/cli.py` (subcommand name tuple ~cli.py:21; dispatch dict ~cli.py:30)
- Modify: `mcpbrain/onboarding.py` (add `bootstrap_main`)
- Test: `tests/test_onboarding_doctor_cli.py`

**Interfaces:**
- Consumes: `ControlClient.bootstrap_baseline` (Task 5).
- Produces: `doctor` reports a "Baseline" line (runs the injected `baseline` repair, degrading if the daemon is down); `mcpbrain bootstrap` runs the baseline via the control API and prints the summary; `onboarding.bootstrap_main(argv) -> int`.

- [ ] **Step 1: Write the failing test**

Create `tests/test_onboarding_doctor_cli.py`:

```python
from mcpbrain import doctor, onboarding


def _healthy_conns():
    ok = {"state": "ok", "detail": "fine"}
    return {"claude": ok, "records": ok, "google": ok,
            "enrichment": ok, "backup": ok}


def test_doctor_reports_baseline_line(capsys):
    calls = []
    repairs = {"baseline": lambda: calls.append(1) or {"status": "done"}}
    code, msg = doctor.run_doctor(
        "/tmp/home", conns=_healthy_conns(), repairs=repairs,
        model_present=lambda _h: True)
    assert "Baseline" in msg
    assert "done" in msg
    assert calls == [1]


def test_doctor_baseline_degrades_when_daemon_down(capsys):
    def _boom():
        raise RuntimeError("daemon not running")
    code, msg = doctor.run_doctor(
        "/tmp/home", conns=_healthy_conns(), repairs={"baseline": _boom},
        model_present=lambda _h: True)
    assert "Baseline" in msg          # reported, not fatal
    assert code == 0                  # a down daemon is not an actionable fault here


def test_bootstrap_main_prints_summary(tmp_path, monkeypatch, capsys):
    from mcpbrain import control_client

    class _FakeCC:
        def __init__(self, home, timeout=5.0): pass
        def bootstrap_baseline(self): return {"status": "done", "cache_hits": 7}
    monkeypatch.setattr(control_client, "ControlClient", _FakeCC)
    rc = onboarding.bootstrap_main([])
    out = capsys.readouterr().out
    assert rc == 0
    assert '"status": "done"' in out and '"cache_hits": 7' in out


def test_bootstrap_main_handles_daemon_down(tmp_path, monkeypatch, capsys):
    from mcpbrain import control_client

    class _FakeCC:
        def __init__(self, home, timeout=5.0): pass
        def bootstrap_baseline(self):
            raise control_client.DaemonUnavailable("no port")
    monkeypatch.setattr(control_client, "ControlClient", _FakeCC)
    rc = onboarding.bootstrap_main([])
    assert rc == 1
    assert "not running" in capsys.readouterr().out
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_onboarding_doctor_cli.py -v`
Expected: FAIL — no "Baseline" line in doctor output; `AttributeError: module 'mcpbrain.onboarding' has no attribute 'bootstrap_main'`.

- [ ] **Step 3: Write minimal implementation**

**(3a)** In `mcpbrain/doctor.py`, add a `baseline` repair inside `_default_repairs` (before its `return`, ~doctor.py:96):

```python
    def _repair_baseline():
        # Re-run the baseline bootstrap via the running daemon (which owns the
        # store + Google services). Degrades if the daemon is down.
        from mcpbrain.control_client import ControlClient, DaemonUnavailable
        try:
            return ControlClient(home, timeout=600).bootstrap_baseline()
        except DaemonUnavailable:
            return {"status": "skipped", "reason": "daemon not running"}
```

and add it to the returned dict:

```python
    return {"daemon": _repair_daemon, "agent": _repair_agent,
            "records": _repair_records, "embedder": _repair_embedder,
            "baseline": _repair_baseline}
```

**(3b)** In `run_doctor`, after the embedder block and before the scheduled-tasks block (~doctor.py:238), add:

```python
    # Baseline bootstrap: re-runnable import of the org snapshot + shared-drive
    # ingest caches. Injected so tests don't hit the network; a down daemon or an
    # unreachable fleet is a graceful skip (➖), never an actionable fault.
    baseline = repairs.get("baseline")
    if baseline is None:
        lines.append(f"➖ {'Baseline':<16} not checked")
    else:
        try:
            res = baseline() or {}
            st = res.get("status", "unknown")
            glyph = "✅" if st in ("done", "skipped") else "❌"
            lines.append(f"{glyph} {'Baseline':<16} bootstrap {st}"
                         + (f" ({res['reason']})" if res.get("reason") else ""))
        except Exception as exc:  # noqa: BLE001 — never fatal
            lines.append(f"➖ {'Baseline':<16} skipped ({exc})")
```

**(3c)** In `mcpbrain/onboarding.py`, add the CLI entry:

```python
def bootstrap_main(argv=None) -> int:
    """`mcpbrain bootstrap` — re-run the baseline import via the daemon."""
    import argparse
    from mcpbrain.config import app_dir
    from mcpbrain.control_client import ControlClient, DaemonUnavailable
    argparse.ArgumentParser(prog="mcpbrain bootstrap").parse_args(argv or [])
    try:
        res = ControlClient(str(app_dir()), timeout=600).bootstrap_baseline()
    except DaemonUnavailable:
        print("The mcpbrain daemon is not running; start it with `mcpbrain daemon`.")
        return 1
    print(json.dumps(res, indent=2))
    return 0
```

**(3d)** In `mcpbrain/cli.py`, add `"bootstrap"` to the subcommand name tuple (cli.py:21-24) and a dispatch entry (in the dict at cli.py:30):

```python
        "bootstrap": lambda: __import__(
            "mcpbrain.onboarding", fromlist=["bootstrap_main"]).bootstrap_main(rest),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_onboarding_doctor_cli.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add mcpbrain/doctor.py mcpbrain/cli.py mcpbrain/onboarding.py tests/test_onboarding_doctor_cli.py
git commit -m "feat(onboarding): doctor re-run step + `mcpbrain bootstrap` CLI"
```

---

## Task 7: Phase C exit gate — end-to-end with fakes + full suite

**Files:**
- Test: `tests/test_onboarding_run.py` (extend)

**Interfaces:**
- Consumes: everything above + the Phase 0 harness (`make_fleet`, `LocalDirFleetStorage`).
- Produces: a fleet-level scenario proving the C promise expressible with fakes — a fresh member install runs `run_bootstrap` against a `LocalDirFleetStorage`, the snapshot lands `origin='org'` rows and the drive fake lands cached rows **with the store's own extractor never invoked** (a sentinel proves zero extraction), and a second run is a marker no-op. The genuine A/B end-to-end is Phase D; this is the strongest statement the fakes support.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_onboarding_run.py`:

```python
def test_end_to_end_with_fakes_zero_extraction_and_idempotent(tmp_path):
    from tests.helpers.org_fleet import make_fleet, LocalDirFleetStorage
    members, curator, _ = make_fleet(tmp_path, n_members=1)
    inst = members[0]
    _configure(inst.home)                         # configured + pinned
    fs = LocalDirFleetStorage(tmp_path / "fleet")

    extracted = {"count": 0}                        # sentinel: must stay 0

    def imp(store, fs_):
        with store._connect() as db:
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                       "VALUES('ceo','CEO','person','org')")
        return {"status": "imported", "entity_count": 1}

    def boot(store, fs_, drive_id, pin):
        extracted["count"] += 0                     # cache import != extraction
        with store._connect() as db:
            db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                       f"VALUES('doc-{drive_id}','Doc','document','local')")
        return {"cache_hits": 10, "drive_id": drive_id}

    res = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1", "D2"],
        import_snapshot=imp, bootstrap_drive=boot)

    assert res["status"] == "done"
    assert res["cache_hits"] == 20
    assert extracted["count"] == 0                  # nothing extracted on cache hits
    with inst.store._connect() as db:
        origins = dict(db.execute("SELECT id, origin FROM entities").fetchall())
    assert origins["ceo"] == "org"                  # snapshot skeleton present
    assert origins["doc-D1"] == "local"             # cache-imported rows present

    # Re-run: marker makes it a no-op (no duplicate work).
    res2 = onboarding.run_bootstrap(
        str(inst.home), inst.store, fleet_storage=fs, drives=["D1", "D2"],
        import_snapshot=imp, bootstrap_drive=boot)
    assert res2["status"] == "skipped"
```

- [ ] **Step 2: Run test to verify it fails then passes**

Run: `python -m pytest tests/test_onboarding_run.py::test_end_to_end_with_fakes_zero_extraction_and_idempotent -v`
Expected: PASS immediately (asserts accumulated behaviour). If it fails, an earlier task regressed — fix there.

- [ ] **Step 3: Run the full suite as the exit gate**

Run: `python -m pytest tests/ -q`
Expected: the whole suite passes (no regressions from the daemon/control/doctor/cli edits).

- [ ] **Step 4: Commit**

```bash
git add tests/test_onboarding_run.py
git commit -m "test(onboarding): Phase C exit gate — fleet end-to-end with fakes + idempotence"
```

- [ ] **Step 5: Phase C complete — hand off to convergence**

Phase C is green in isolation against the shared harness. Convergence (Phase D) swaps the fakes for the real A/B calls (see Self-Review). No push/release.

---

## Self-Review

**Spec coverage** (subsystem C items from spec §"Subsystem C — onboarding integration"):

| Spec C-item | Task(s) |
|---|---|
| Baseline-bootstrap step run **before first sync** in `mcpbrain setup` (via wizard → daemon first cycle) | Task 4 (`run_one` calls `bootstrap_baseline_once` before `run_cycle`) — real entrypoint documented in the Architecture note |
| …and **re-runnable via `doctor`** | Task 6 (doctor "Baseline" block) + Task 5 (`/api/bootstrap-baseline`) + Task 6 (`mcpbrain bootstrap`) |
| 1. Detect fleet folder → download+import the org-graph snapshot (instant layer-1 graph) | Task 1 (snapshot step, first) + Task 2 (`_fleet_folder_id`, `_default_import_snapshot`) + Task 3 (`org_import_enabled` gate) |
| 2. Enumerate accessible shared drives → bulk-import `.mcpbrain-cache/` artifacts per cache hit | Task 1 (per-drive step) + Task 2 (`_default_enumerate_drives`, `_default_bootstrap_drive`) + Task 3 (`ingest_cache` gate + pin) |
| 3. Normal sync then runs; only cache-misses cost extraction/embedding/enrichment | Task 4 (ordering: bootstrap precedes `run_cycle`) + Task 7 (zero-extraction sentinel with fakes) |
| Idempotent, safe to re-run | Task 3 (marker + resume) + Task 4 (in-proc latch) + Task 7 (re-run no-op) |
| Degrade gracefully (no fleet folder / no snapshot / no pin → skip cleanly, proceed to normal sync) | Task 1 (no-transport / no-pin / no-snapshot / bad-drive / snapshot-error branches) + Task 3 (`degraded` status, retryable) + Task 4 (never raises into cycle) |

**The injection seam (the parallelism-enabling decision):** `bootstrap_baseline` takes `import_snapshot` and `bootstrap_drive` as injected keyword args; `run_bootstrap` additionally injects `make_fleet_storage` and `enumerate_drives`. All four have default bindings in `onboarding.py` that **lazily import** the real A/B modules and **degrade** (`"unavailable"` / `None` / `[]`) while those modules are unbuilt. C imports and tests fully today with fakes; at Phase D the real modules land and the defaults activate with no change to C's orchestration logic.

**Exactly which fakes Phase D must replace with real A/B calls:**
1. `import_snapshot(store, fleet_storage) -> dict` — Phase D removes the `ImportError` guard's reachability by shipping **B's `mcpbrain/org_import.py`**. C's `_default_import_snapshot` already calls it; verify B's return contract matches C's expectation (`status in {"imported","unchanged","no_snapshot"}`; else treated as error).
2. `bootstrap_drive(store, fleet_storage, drive_id, pin) -> dict` — shipped by **A's `mcpbrain/ingest_cache.py`**; C's `_default_bootstrap_drive` calls it. Verify it returns an integer `cache_hits`.
3. `_default_make_fleet_storage` → **A's `DriveFleetStorage(drive_service, folder_id)`** (the prod `FleetStorage`). C imports `mcpbrain.ingest_cache.DriveFleetStorage`; if A names it differently, this one function is the single adaptation point.
4. `_default_enumerate_drives` → **A's shared-drive enumeration** (C imports `mcpbrain.ingest_cache.list_shared_drive_ids`); if A's symbol differs, adapt here only.

Phase D's C end-to-end test (spec §Testing "End-to-end (Phase D)") replaces the Task 1/3/7 fakes with these real calls over a real (or A-faked-Drive) `FleetStorage`, asserting a new-user bootstrap yields a working brain with zero extraction on cached content.

**Non-collision with A/B/Phase 0:** C creates one new module (`onboarding.py`) + its tests; the daemon edits are C's own logic (a first-cycle call + a method + an init flag), touching **none** of the frozen Phase 0 cadence registration slots, no schema, no config accessor, no `fleet_pin`. The marker is a plain `<home>/baseline_bootstrap.json` file — not config, not schema. The `/api/bootstrap-baseline` route, the client method, the CLI verb, and the doctor line are all additive.

**Placeholder scan:** No TBD/TODO; every step ships complete code. The only intentional "not-yet-real" points are the four default bindings, which are complete, tested, and documented as the convergence seam.

**Type/name consistency:** `bootstrap_baseline` signature identical across Task 1 (def), Task 3 (`run_bootstrap` call), Task 7 (tests). Summary dict keys (`snapshot`, `drives`, `snapshot_done`, `done_drive_ids`, `cache_hits`, `errors`, `status`) consistent across orchestrator, glue, endpoint, and tests. `done_drive_ids` is a `set` inside the orchestrator and serialized to a sorted list by `run_bootstrap` (for JSON/marker). Marker keys (`snapshot_done`, `done_drive_ids`, `completed_at`, `cache_hits`) match between `run_bootstrap` writer and Task 3 test readers. Daemon attribute `_baseline_bootstrap_done` and method `bootstrap_baseline_once` consistent across `__init__`, `run_one`, the method, and Task 4/5 tests. Endpoint path `/api/bootstrap-baseline` identical in Task 5 route, client, and CLI/doctor callers.
</content>
</invoke>
