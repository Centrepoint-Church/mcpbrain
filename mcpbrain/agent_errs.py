"""Surface records-cadence launchd agent stderr as proactive findings.

The records cadence agents (prune daily, context-health weekly) write
stderr to ~/.mcpbrain/com.mcpbrain.records.*.err. Nothing reads those
files, so a crashing agent rots unseen. check_agent_errs tails each .err file
per cycle, using a byte-offset cursor in sync_cursors, and turns new non-empty
stderr into an open finding on the same surface Phase 1 built (record_finding).

Findings dedupe on UNIQUE(finding_type, ref_id). ref_id encodes the filename
plus a hash of the new content, so the identical recurring error collapses to
one finding while a different message opens a new one. Audit finding R2.
"""
from __future__ import annotations

import hashlib
import logging
from pathlib import Path

log = logging.getLogger(__name__)

FINDING_TYPE = "agent_stderr"
GLOB = "com.mcpbrain.records.*.err"
# Cap each finding's detail at the last 4KB of the new region — a tail is enough
# to diagnose; we don't want a runaway log filling the findings table.
TAIL_BYTES = 4096


def _agent_label(filename: str) -> str:
    """com.mcpbrain.records.prune.err -> com.mcpbrain.records.prune"""
    return filename[:-4] if filename.endswith(".err") else filename


def check_agent_errs(store, home) -> None:
    """Scan records cadence .err files; record new stderr as findings.

    Never raises: a bad/unreadable .err file is logged and skipped so the sync
    cycle keeps running (matches the capture-drain isolation in run_cycle).
    """
    home = Path(home)
    try:
        err_files = sorted(home.glob(GLOB))
    except OSError as exc:  # pragma: no cover - home itself unreadable
        log.warning("agent_errs: cannot scan %s: %s", home, exc)
        return

    for path in err_files:
        try:
            _check_one(store, path)
        except Exception as exc:  # noqa: BLE001 — one bad file must not break the cycle
            log.warning("agent_errs: skipped %s (%s)", path.name, exc)


def _check_one(store, path: Path) -> None:
    filename = path.name
    cursor_key = f"agent_err:{filename}"

    if not path.exists():
        return
    size = path.stat().st_size

    raw = store.get_cursor(cursor_key)
    cursor = int(raw) if raw and raw.isdigit() else 0

    if size < cursor:
        # Truncated or rotated: treat the whole file as new.
        cursor = 0
    if size == cursor:
        return

    with open(path, "rb") as fh:
        fh.seek(cursor)
        new_region = fh.read()

    # Hash the FULL new region before capping so two distinct multi-KB errors
    # that share the same last 4KB still produce different fingerprints.
    fp = hashlib.sha256(new_region).hexdigest()[:12]

    # Cap the stored detail at the last TAIL_BYTES — enough to diagnose without
    # letting a runaway log fill the findings table.
    if len(new_region) > TAIL_BYTES:
        new_region = new_region[-TAIL_BYTES:]
    content = new_region.decode("utf-8", errors="replace").strip()

    if not content:
        # Whitespace-only growth: advance cursor, no finding.
        # (fp is already computed above; we just don't record the finding.)
        store.set_cursor(cursor_key, str(size))
        return
    label = _agent_label(filename)
    # record_finding upserts on UNIQUE(finding_type, ref_id) and clears
    # resolved_at, so a dismissed finding coming back means the agent is still
    # failing — that's intentional: recurrence reopens the finding by design.
    store.record_finding(
        FINDING_TYPE,
        ref_id=f"{filename}:{fp}",
        summary=f"joshbrain agent stderr: {label}",
        detail=content,
        severity="warn",
    )
    store.set_cursor(cursor_key, str(size))
