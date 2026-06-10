"""Cross-platform bodies for the `mcpbrain session-start` / `session-end` hooks.

session-start prints bounded priming context (recent hot.md + open actions) to
stdout; Claude Code injects it into the session. session-end reads the hook JSON
from stdin, parses the transcript, and queues a one-line session capture — but
only for substantial interactive sessions, so trivial/headless runs add no noise.
Neither ever hard-fails a session.
"""
from __future__ import annotations

import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from mcpbrain import config
from mcpbrain.capture import write_capture

_MAX_LINES = 8
_MIN_TURNS = 2          # at least this many user turns to count as "substantial"
_MIN_CHARS = 200        # ...or this much user text


def session_start(home: str, out=None) -> None:
    out = out or sys.stdout
    print("## Recent continuity (hot.md)", file=out)
    try:
        hot = Path(config.records_dir(home)) / "state" / "hot.md"
        lines = [ln for ln in hot.read_text().splitlines()
                 if ln.startswith("- **20")][:_MAX_LINES]
        print("\n".join(lines) if lines else "(none)", file=out)
    except OSError:
        print("(none)", file=out)
    print("\n## Open actions", file=out)
    print(_open_actions(home), file=out)


def _open_actions(home: str) -> str:
    try:
        port = (Path(home) / "control_port").read_text().strip()
        token = (Path(home) / "control_token").read_text().strip()
    except OSError:
        return "(actions unavailable)"
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/api/dashboard/today",
        headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=3) as r:  # noqa: S310 loopback only
            data = json.loads(r.read() or b"{}")
    except Exception:  # noqa: BLE001 — daemon down / no dashboard
        return "(actions unavailable)"
    actions = (data or {}).get("actions", {}) or {}
    rows = []
    for bucket in ("overdue", "due_today", "upcoming"):
        for x in (actions.get(bucket) or []):
            t = (x.get("text") or "").strip().replace("\n", " ")
            if t:
                rows.append(f"- [{bucket}] {t[:80]}")
    return "\n".join(rows[:_MAX_LINES]) if rows else "(no open actions)"


def session_end(home: str, stdin=None) -> None:
    stdin = stdin or sys.stdin
    try:
        hook = json.loads(stdin.read() or "{}")
    except (ValueError, OSError):
        return
    tpath = hook.get("transcript_path") or ""
    if not tpath:
        return
    try:
        raw = Path(tpath).read_text()
    except OSError:
        return
    user_texts = []
    for line in raw.splitlines():
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        msg = ev.get("message") or {}
        if msg.get("role") == "user":
            c = msg.get("content")
            if isinstance(c, str):
                user_texts.append(c)
            elif isinstance(c, list):
                user_texts.extend(b.get("text", "") for b in c if isinstance(b, dict))
    joined = " ".join(t.strip() for t in user_texts if t.strip())
    if len(user_texts) < _MIN_TURNS and len(joined) < _MIN_CHARS:
        return  # trivial / headless single-shot -> skip
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    envelope = {
        "kind": "ingest",
        "source": "session_end_hook",
        "captured_at": stamp,
        "title": f"Session {hook.get('session_id', 'unknown')[:8]} {stamp[:10]}",
        "content": joined[:2000],
        "tags": "session",
        "observation_type": "note",
    }
    try:
        write_capture(home, envelope)
    except (ValueError, OSError):
        return


def session_start_main(argv=None) -> int:
    session_start(str(config.app_dir()))
    return 0


def session_end_main(argv=None) -> int:
    session_end(str(config.app_dir()))
    return 0
