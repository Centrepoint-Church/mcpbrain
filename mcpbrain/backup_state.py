"""``backup_state.json``: the one durable record of whether a backup LANDED.

Moved out of ``daemon`` so the incremental uploader can write it without
importing the daemon; ``daemon.write_backup_state`` re-exports it for existing
callers.
"""
from __future__ import annotations

import json
import logging
import os
import tempfile
import time
from pathlib import Path

log = logging.getLogger(__name__)


def write_backup_state(home, *, ok: bool, error: str | None = None,
                       restore_point: float | None = None) -> dict:
    """Record the outcome of a backup ATTEMPT to ``backup_state.json``.

    The encrypted artifact is written locally BEFORE the upload, so
    ``snapshot.enc``'s mtime is refreshed by every failed run and cannot
    distinguish "backed up" from "encrypted, then failed to upload". Uploads
    fail intermittently rather than outright (97 failures / 52 successes,
    2026-06-25 -> 2026-08-04, arriving in storms of dozens), which is exactly
    the shape an mtime check cannot see: the probe read "Backup: On" straight
    through the 2026-08-03 storm when nothing was reaching Drive. This file is
    the only durable record of whether a backup actually LANDED;
    ``probes.probe_backup`` keys off it.

    ``restore_point`` is the CAPTURE time (wall clock) of the newest committed
    restore point. With incremental backup a commit can land hours after its
    capture (a laptop offline overnight), so ``last_success`` (commit time)
    alone overstates how fresh the backed-up data is. It is carried forward
    unchanged by any write that does not pass one.

    Wall-clock, not the injected monotonic clock: the value is persisted and
    compared across restarts, where a monotonic epoch is meaningless (same
    reasoning as ``_recent_watchdog_exits``). Best-effort — a write failure must
    never fail an otherwise-good backup.

    Returns:
        The ``state`` dict just written (``last_attempt``, ``last_success``,
        ``consecutive_failures``, ``last_error``, ``restore_point``) — even
        when the write to disk itself failed (best-effort covers persistence,
        not the return). This lets a caller such as ``maybe_backup`` read e.g.
        ``consecutive_failures`` off the return value directly, without a
        second read of ``backup_state.json`` through another module's
        private helper.
    """
    path = Path(home) / "backup_state.json"
    try:
        prev = json.loads(path.read_text())
        if not isinstance(prev, dict):
            prev = {}
    except (OSError, ValueError):
        prev = {}
    now = time.time()
    try:
        prev_failures = int(prev.get("consecutive_failures") or 0)
    except (TypeError, ValueError):
        prev_failures = 0
    state = {
        "last_attempt": now,
        "last_success": now if ok else prev.get("last_success"),
        "consecutive_failures": 0 if ok else prev_failures + 1,
        "last_error": None if ok else error,
        "restore_point": (restore_point if restore_point is not None
                          else prev.get("restore_point")),
    }
    # Atomic write (temp file in the same dir + os.replace): a reader
    # (probes._read_backup_state, last_backup_attempt_epoch) must never see a
    # truncated or half-written file -- a crash or concurrent read mid-write
    # over an in-place write_text would otherwise corrupt the ONLY durable
    # record of whether a backup actually landed, and probes.py's own
    # corrupt-file handling would then read it as "no recent backup" (see
    # `_read_backup_state`).
    try:
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".backup_state.",
                                   suffix=".tmp")
        try:
            with os.fdopen(fd, "w") as f:
                f.write(json.dumps(state))
            os.replace(tmp, path)
        except OSError:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
    except OSError as exc:
        log.warning("backup state write failed (continuing): %s", exc)
    return state
