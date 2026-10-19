"""`mcpbrain backup`: back up now, and watch it finish.

The daemon owns the store (single writer) and the bulk lock a snapshot needs, so
this command does not back up in its own process: it asks the running daemon to
(POST /api/backup/run), then follows /api/status (`backup_running`) and
backup_state.json (`last_attempt`) until the attempt it asked for is recorded.
Ctrl-C stops watching, never the backup.
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from mcpbrain.config import app_dir
from mcpbrain.control_client import ControlClient, DaemonUnavailable

# Long enough for a multi-GB snapshot plus a slow upload with resumes. Past it
# the command stops watching; the backup carries on in the daemon.
WAIT_LIMIT_S = 3 * 3600
POLL_S = 5.0


def _last_attempt(home) -> float:
    try:
        state = json.loads((Path(home) / "backup_state.json").read_text())
        return float(state.get("last_attempt") or 0.0)
    except (OSError, ValueError, TypeError, AttributeError):
        return 0.0


def _read_state(home) -> dict:
    try:
        return json.loads((Path(home) / "backup_state.json").read_text())
    except (OSError, ValueError):
        return {}


def _mb(n) -> str:
    return f"{(n or 0) / 1_000_000:,.0f} MB"


def _describe(running: dict) -> str:
    if running.get("phase") == "upload" and running.get("total"):
        pct = int(100 * (running.get("sent") or 0) / running["total"])
        return (f"Uploading to Google Drive: {_mb(running.get('sent'))} of "
                f"{_mb(running['total'])} ({pct}%)")
    if running.get("phase") == "upload":
        return "Uploading to Google Drive"
    return "Taking an encrypted snapshot of the brain"


def main(argv=None, *, client=None, home=None, sleep=time.sleep,
         clock=time.monotonic, out=print) -> int:
    p = argparse.ArgumentParser(prog="mcpbrain backup",
                                description="Back up the brain to Google Drive now.")
    p.add_argument("--no-wait", action="store_true",
                   help="ask the daemon to back up, then return without watching")
    args = p.parse_args(argv or [])
    home = home or app_dir()
    client = client or ControlClient()

    before = _last_attempt(home)
    try:
        resp = client.request_backup()
    except DaemonUnavailable:
        try:
            client.status()
        except DaemonUnavailable:
            out("The mcpbrain daemon isn't running, so it can't back up. "
                "Run: mcpbrain doctor")
            return 1
        out("The running daemon is an older version without this command. "
            "Run: mcpbrain update, then try again.")
        return 1
    if resp.get("error"):
        out("Backup isn't set up on this machine yet. "
            "Turn it on in the mcpbrain wizard, then try again.")
        return 1

    out("Backup requested. The daemon starts it once its current sync cycle "
        "finishes, usually within a few minutes.")
    if args.no_wait:
        return 0

    shown = None
    last_pct = -10
    deadline = clock() + WAIT_LIMIT_S
    try:
        while clock() < deadline:
            if _last_attempt(home) > before:
                state = _read_state(home)
                if (state.get("consecutive_failures") or 0) == 0:
                    out("✅ Backup complete.")
                    return 0
                out(f"⚠️  Backup failed: {state.get('last_error') or 'unknown error'}")
                out("The daemon retries automatically within the hour. "
                    "Run `mcpbrain backup` again to retry now.")
                return 1
            try:
                running = client.status().get("backup_running")
            except DaemonUnavailable:
                running = None
            if running:
                line = _describe(running)
                pct = (int(100 * (running.get("sent") or 0) / running["total"])
                       if running.get("phase") == "upload" and running.get("total")
                       else None)
                if running.get("phase") != shown or (pct is not None
                                                     and pct >= last_pct + 10):
                    out(line)
                    shown = running.get("phase")
                    if pct is not None:
                        last_pct = pct - pct % 10
            sleep(POLL_S)
    except KeyboardInterrupt:
        out("\nStopped watching. The backup carries on in the background; "
            "check it with: mcpbrain doctor")
        return 0
    out("Still running after a long time. It carries on in the background; "
        "check it with: mcpbrain doctor")
    return 0
