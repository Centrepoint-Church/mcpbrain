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
