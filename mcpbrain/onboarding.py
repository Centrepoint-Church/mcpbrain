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
import os
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


def _fleet_folder_id(home) -> str:
    """The fleet folder id: local config's fleet.folder_id, else the baked-in
    org default (org_defaults.FLEET_FOLDER_ID)."""
    from mcpbrain import config, org_defaults
    fleet = config.read_config(home).get("fleet") or {}
    return fleet.get("folder_id") or org_defaults.FLEET_FOLDER_ID


def _default_make_fleet_storage(home, drive_service):
    """Build the prod fleet-FOLDER FleetStorage (subsystem A's factory) for the
    org-graph snapshot. Degrades to None with no drive service, no fleet folder,
    or if A is not importable."""
    if drive_service is None:
        return None
    try:
        from mcpbrain.fleet_storage import fleet_folder_storage  # subsystem A
    except ImportError:
        return None
    return fleet_folder_storage(home, drive_service)


def _default_make_drive_storage(drive_service):
    """Return a factory building a per-shared-drive cache FleetStorage (rooted at
    that drive's `.mcpbrain-cache/`) — this is what `bootstrap_drive` reads, NOT
    the fleet-folder storage. Degrades to a None-returning factory if A is
    unavailable or there is no drive service."""
    if drive_service is None:
        return lambda drive_id: None
    try:
        from mcpbrain.fleet_storage import drive_cache_storage  # subsystem A
    except ImportError:
        return lambda drive_id: None
    return lambda drive_id: drive_cache_storage(drive_service, drive_id)


def _default_enumerate_drives(drive_service) -> list[str]:
    """Enumerate accessible shared-drive ids (subsystem A returns dicts with id
    + name; map to ids). Degrades to [] with no service or if A is unavailable."""
    if drive_service is None:
        return []
    try:
        from mcpbrain.fleet_storage import list_shared_drives  # subsystem A (re-export)
    except ImportError:
        return []
    return [d["id"] for d in list_shared_drives(drive_service) if d.get("id")]


# -- orchestrator (pure; no config, no I/O beyond the injected callables) ---

_SNAPSHOT_OK = {"imported", "unchanged"}
_DRIVE_OK = {"ok"}


def bootstrap_baseline(store, fleet_storage, drives, pin, *,
                       import_snapshot=_default_import_snapshot,
                       bootstrap_drive=_default_bootstrap_drive,
                       make_drive_storage=None,
                       done_drive_ids=(), snapshot_done=False) -> dict:
    """Import the org snapshot then each drive's ingest cache. Ordering is a
    contract: snapshot first. Degrades cleanly (no transport / no pin / no
    snapshot / a bad drive) and resumes (done_drive_ids / snapshot_done).

    `fleet_storage` is the fleet-FOLDER storage (holds the org-graph snapshot).
    Per-drive ingest cache lives in each shared drive's own `.mcpbrain-cache/`,
    so `make_drive_storage(drive_id) -> FleetStorage | None` supplies the right
    storage for `bootstrap_drive`. When None (e.g. fake-injecting tests), the
    fleet-folder storage is reused as a fallback."""
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
        drive_fs = make_drive_storage(drive_id) if make_drive_storage else fleet_storage
        if drive_fs is None:
            result["drives"][drive_id] = {"status": "skipped", "reason": "no_drive_storage"}
            continue
        try:
            detail = bootstrap_drive(store, drive_fs, drive_id, pin) or {}
            status = detail.get("status", "ok")
            result["drives"][drive_id] = {"status": status, "detail": detail}
            result["cache_hits"] += int(detail.get("cache_hits", 0) or 0)
            if status in _DRIVE_OK:
                done.add(drive_id)
        except Exception as exc:  # noqa: BLE001 — one bad drive never blocks the rest
            log.warning("baseline cache import failed for %s: %s", drive_id, exc,
                        exc_info=True)
            result["drives"][drive_id] = {"status": "error", "detail": str(exc)}
            errors.append(f"drive {drive_id}: {exc}")

    return result


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
    # Atomic write (tempfile + os.replace, mirroring config.write_config): a crash
    # or concurrent writer must never leave a truncated marker that _read_marker
    # then discards, silently re-running the whole bootstrap and re-importing every
    # already-done drive.
    try:
        p = _marker_path(home)
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(data, indent=2))
        os.replace(tmp, p)
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
                  make_drive_storage=None,
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
    # Build the per-drive cache-storage factory only on the real path (a Drive
    # service is present). When callers inject fleet_storage directly without a
    # service, leave it None so bootstrap_baseline falls back to that storage.
    if make_drive_storage is None and drive_service is not None:
        make_drive_storage = _default_make_drive_storage(drive_service)

    prev_done = set(marker.get("done_drive_ids") or [])
    prev_snapshot = bool(marker.get("snapshot_done"))
    summary = bootstrap_baseline(
        store, fleet_storage, list(drives), pin,
        import_snapshot=import_snapshot, bootstrap_drive=bootstrap_drive,
        make_drive_storage=make_drive_storage,
        done_drive_ids=prev_done,
        # If import is off, treat the snapshot as already handled (skip it).
        snapshot_done=prev_snapshot or not import_on)

    # Finalize the marker only when the baseline is actually IN PLACE, not merely
    # when a transport existed. The likeliest incomplete states during rollout are
    # not "no transport": the curator hasn't published a snapshot yet
    # (status=no_snapshot), or the fleet_secret isn't distributed yet (pin absent
    # => every drive skipped no_pin). Stamping completed_at then would latch the
    # install "done" on a run that imported nothing, and should_bootstrap would
    # never retry once the baseline lands — silently starving the exact
    # early-adopter population the feature serves.
    snapshot_ok = (not import_on) or summary["snapshot_done"]
    attempted = set(summary["drives"].keys())
    done = set(summary["done_drive_ids"])
    drives_ok = (not cache_on) or attempted.issubset(done)  # all enumerated drives imported
    completed = fleet_storage is not None and snapshot_ok and drives_ok
    _write_marker(home, {
        "snapshot_done": summary["snapshot_done"],
        "done_drive_ids": sorted(summary["done_drive_ids"]),
        "cache_hits": summary.get("cache_hits", 0),
        "completed_at": _utcnow_iso() if completed else "",
    })
    summary["status"] = ("done" if completed
                         else "degraded" if fleet_storage is None else "pending")
    # done_drive_ids is a set (from the orchestrator) — make it JSON-friendly for
    # any caller that serializes the summary (control API).
    summary["done_drive_ids"] = sorted(summary["done_drive_ids"])
    return summary


# -- CLI entry ----------------------------------------------------------------

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
