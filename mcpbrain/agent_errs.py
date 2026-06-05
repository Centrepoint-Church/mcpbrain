"""Surface joshbrain launchd agent stderr as proactive findings.

The two joshbrain launchd agents (prune daily, context-health weekly) write
stderr to ~/.mcpbrain/church.centrepoint.joshbrain.*.err. Nothing reads those
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
GLOB = "church.centrepoint.joshbrain.*.err"
# Cap each finding's detail at the last 4KB of the new region — a tail is enough
# to diagnose; we don't want a runaway log filling the findings table.
TAIL_BYTES = 4096


def _agent_label(filename: str) -> str:
    """church.centrepoint.joshbrain.prune.err -> church.centrepoint.joshbrain.prune"""
    return filename[:-4] if filename.endswith(".err") else filename


def check_agent_errs(store, home) -> None:
    """Scan joshbrain .err files; record new stderr as findings.

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

    # Cap at the last TAIL_BYTES of the new region.
    if len(new_region) > TAIL_BYTES:
        new_region = new_region[-TAIL_BYTES:]
    content = new_region.decode("utf-8", errors="replace").strip()

    if not content:
        # Whitespace-only growth: advance cursor, no finding.
        store.set_cursor(cursor_key, str(size))
        return

    fp = hashlib.sha256(content.encode("utf-8")).hexdigest()[:12]
    label = _agent_label(filename)
    store.record_finding(
        FINDING_TYPE,
        ref_id=f"{filename}:{fp}",
        summary=f"joshbrain agent stderr: {label}",
        detail=content,
        severity="warn",
    )
    store.set_cursor(cursor_key, str(size))
