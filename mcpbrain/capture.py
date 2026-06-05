"""Capture spool writer: MCP write tools call this from the MCP server process.

Envelopes are validated then written atomically (temp + os.replace) into
MCPBRAIN_HOME/capture_inbox/. The daemon drains them on its cycle
(drain.drain_captures). This module never opens the database — the daemon is
the single writer; this is the whole reason the spool exists.
"""
from __future__ import annotations

import json
import os
import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mcpbrain.contract import validate_capture


def write_capture(home, envelope: dict) -> Path:
    """Validate and atomically write one capture envelope. Returns the path.

    Raises ValueError on an invalid envelope so MCP tools can return the
    problem to the caller instead of spooling junk for the drain to quarantine.
    """
    problems = validate_capture(envelope)
    if problems:
        raise ValueError("; ".join(problems))
    inbox = Path(home) / "capture_inbox"
    inbox.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%f")
    name = f"cap-{stamp}-{secrets.token_hex(3)}.json"
    fd, tmp = tempfile.mkstemp(dir=str(inbox), prefix=".cap.", suffix=".tmp")
    try:
        # Deliberate: no fsync before os.replace. This is a capture queue, not a
        # durability log. A power-cut may drop the most recent capture, which is
        # an acceptable loss here; fsync per write would add latency to every
        # MCP capture call for no real benefit. A partially-written tmp file
        # never reaches the inbox (os.replace is atomic), and any malformed
        # envelope that does land is caught by the drain's quarantine path.
        with os.fdopen(fd, "w") as f:
            json.dump(envelope, f, ensure_ascii=False)
        target = inbox / name
        os.replace(tmp, target)
        return target
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
