"""Bound the launchd agent logs so a failure loop cannot fill the disk.

launchd writes StandardOutPath/StandardErrorPath directly and rotates NOTHING —
macOS leaves that to newsyslog, which mcpbrain does not configure. So any
recurring error grows the file forever. On the author's machine a permission
failure that retried every sync cycle produced 98,964 tracebacks and a 658 MB
`com.mcpbrain.err` on a volume that was 99% full, and nothing anywhere noticed.

Truncation, not rotation, and deliberately so: launchd holds the file open with
O_APPEND, so renaming the file leaves the daemon writing to the now-invisible
old inode forever. Truncating in place keeps the same inode, and the writer
simply continues from offset 0.

The tail is preserved because the newest lines are the diagnostic ones — losing
them to reclaim space would trade one silent failure for another.
`agent_errs.check_agent_errs` already treats a file smaller than its cursor as
"truncated or rotated: treat the whole file as new", so capping cooperates with
it rather than confusing it.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Files launchd owns. Both streams of the daemon and of every records agent.
GLOBS = ("com.mcpbrain*.log", "com.mcpbrain*.err")

MAX_BYTES = 32 * 1024 * 1024      # cap per file
KEEP_BYTES = 2 * 1024 * 1024      # tail retained when capping


def cap_agent_logs(home, *, max_bytes: int = MAX_BYTES,
                   keep_bytes: int = KEEP_BYTES) -> int:
    """Truncate over-cap launchd logs, keeping their tail. Returns bytes freed.

    Never raises: a log that cannot be capped must not take down the cycle that
    called it — that would be strictly worse than the disk usage it prevents.
    """
    freed = 0
    home = Path(home)
    for pattern in GLOBS:
        for path in sorted(home.glob(pattern)):
            try:
                size = path.stat().st_size
                if size <= max_bytes:
                    continue
                with open(path, "rb") as fh:
                    fh.seek(max(0, size - keep_bytes))
                    tail = fh.read()
                stamp = datetime.now(timezone.utc).isoformat(timespec="seconds")
                banner = (f"\n--- mcpbrain: log capped at {stamp}; "
                          f"{size} bytes truncated to the last {len(tail)} ---\n")
                # Truncate in place (same inode, launchd keeps appending), then
                # write the tail back. A concurrent daemon write can interleave
                # here at worst as one garbled line, which is an acceptable
                # trade for not letting the file grow without bound.
                os.truncate(path, 0)
                with open(path, "ab") as fh:
                    fh.write(banner.encode() + tail)
                freed += size - (len(tail) + len(banner))
                log.warning("log_cap: %s was %.0f MB; truncated to its last %.0f MB",
                            path.name, size / 1024**2, len(tail) / 1024**2)
            except OSError as exc:
                log.warning("log_cap: could not cap %s: %s", path, exc)
    return freed
