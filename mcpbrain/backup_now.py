"""`mcpbrain backup`: back up now, and watch it finish.

The daemon owns the store (single writer) and the bulk lock a capture needs, so
this command does not back up in its own process: it asks the running daemon to
(POST /api/backup/run), then follows /api/status (`backup_running`,
`backup_requested`, `backup.pending`) and backup_state.json until the capture it
asked for has been taken, everything waiting has uploaded, and `last_success`
has moved past the request. Ctrl-C stops watching, never the backup.
"""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path

from mcpbrain.config import app_dir
from mcpbrain.control_client import ControlClient, DaemonUnavailable

# Long enough for a multi-GB first upload with resumes. Past it the command
# stops watching; the backup carries on in the daemon.
WAIT_LIMIT_S = 3 * 3600
POLL_S = 5.0
# Consecutive failed status polls before the command stops watching.
LOST_POLLS = 3


def _read_state(home) -> dict:
    try:
        state = json.loads((Path(home) / "backup_state.json").read_text())
        return state if isinstance(state, dict) else {}
    except (OSError, ValueError):
        return {}


def _num(state: dict, key: str) -> float:
    try:
        return float(state.get(key) or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _mb(n) -> str:
    return f"{(n or 0) / 1_000_000:,.0f}"


def _hhmm(epoch) -> str:
    try:
        return datetime.fromtimestamp(float(epoch)).strftime("%H:%M")
    except (OSError, OverflowError, ValueError, TypeError):
        return "just now"


def _upload_line(running: dict, pending) -> str:
    sent, total = running.get("sent") or 0, running.get("total") or 0
    pct = int(100 * sent / total) if total else 0
    line = f"Uploading: {_mb(sent)} of {_mb(total)} MB ({pct}%)"
    if pending:
        line += f" · {pending} waiting"
    return line


def main(argv=None, *, client=None, home=None, sleep=time.sleep,
         clock=time.monotonic, out=None) -> int:
    p = argparse.ArgumentParser(prog="mcpbrain backup",
                                description="Back up the brain to Google Drive now.")
    p.add_argument("--no-wait", action="store_true",
                   help="ask the daemon to back up, then return without watching")
    args = p.parse_args(argv or [])
    if out is None:
        # Flushed: the command is mostly waiting, and a buffered print would
        # show nothing until the end when stdout is not a terminal.
        def out(line):
            print(line, flush=True)
    home = home or app_dir()
    client = client or ControlClient()

    state0 = _read_state(home)
    before = _num(state0, "last_success")
    before_attempt = _num(state0, "last_attempt")
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

    out("Capturing changes…")
    if args.no_wait:
        return 0

    shown = None
    last_pct = -10
    last_item = None
    told_paused = False
    lost = 0
    deadline = clock() + WAIT_LIMIT_S
    retry = ("mcpbrain keeps retrying in the background. "
             "Run `mcpbrain backup` again to retry now.")
    try:
        while clock() < deadline:
            try:
                status = client.status()
                lost = 0
            except DaemonUnavailable:
                lost += 1
                if lost >= LOST_POLLS:
                    out("Lost contact with mcpbrain; the backup carries on in "
                        "the background. Check it with: mcpbrain doctor")
                    return 0
                sleep(POLL_S)
                continue
            running = status.get("backup_running")
            requested = bool(status.get("backup_requested"))
            info = status.get("backup") or {}
            pending = info.get("pending")
            state = _read_state(home)
            success = _num(state, "last_success")
            err = str(info.get("last_error") or state.get("last_error") or "")
            paused = requested and not running and err.startswith("capture paused")
            if paused and not told_paused:
                out(f"Earlier changes ({_mb(info.get('pending_bytes'))} MB) are "
                    f"still uploading; this backup will start when they finish.")
                told_paused = True
            if not requested and not running and not pending:
                if success > before:
                    out(f"✅ Backed up. Restore point "
                        f"{_hhmm(state.get('restore_point') or success)}.")
                    return 0
                if _num(state, "last_attempt") > before_attempt and \
                        (state.get("consecutive_failures") or 0) > 0:
                    out(f"⚠️  Backup failed: {state.get('last_error') or 'unknown error'}")
                    out(retry)
                    return 1
            if running and running.get("phase") == "upload" and running.get("total"):
                pct = int(100 * (running.get("sent") or 0) / running["total"])
                item = running.get("item")
                if item != last_item:
                    if last_item is not None:
                        out("Uploading the next set of changes"
                            + (f" ({pending} waiting)" if pending else ""))
                    last_item = item
                    last_pct = -10
                    shown = None
                if shown != "upload" or pct >= last_pct + 10:
                    out(_upload_line(running, pending))
                    shown = "upload"
                    last_pct = pct - pct % 10
            elif (not running and not requested and pending
                  and (state.get("consecutive_failures") or 0) > 0
                  and _num(state, "last_attempt") > before_attempt):
                out(f"⚠️  Backup failed: {state.get('last_error') or 'unknown error'}")
                out(retry)
                return 1
            sleep(POLL_S)
    except KeyboardInterrupt:
        out("\nStopped watching. The backup carries on in the background; "
            "check it with: mcpbrain doctor")
        return 0
    out("Still running after a long time. It carries on in the background; "
        "check it with: mcpbrain doctor")
    return 0
