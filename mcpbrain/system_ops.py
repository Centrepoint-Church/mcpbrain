"""System controls behind the console's Settings > System section.

Restart, start at login, update check/apply, in-process diagnostics, log tails
and a redacted support report. The control API (control_api.py) routes to these;
each takes its OS/network seams as injectable arguments so tests never run
launchctl/schtasks, restart the real daemon or hit the network.

The daemon picks a new control port and token on every start, so an open page
cannot follow a restart: the console says so and points at the menu bar.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Log files per name, under MCPBRAIN_HOME. launchd sends stdout to <label>.log
# and stderr (where Python logging writes) to <label>.err; on Windows the
# daemon's RotatingFileHandler writes com.mcpbrain.log.
_LOG_FILES = {
    "daemon": ("com.mcpbrain.err", "com.mcpbrain.log"),
    "tray": ("com.mcpbrain.tray.err", "com.mcpbrain.tray.log"),
}
REPORT_LOG_LINES = 200


def _version() -> str:
    from mcpbrain import __version__
    return str(__version__)


# -- restart -------------------------------------------------------------------

def request_restart(daemon, *, platform: str | None = None, restart=None) -> threading.Thread:
    """Restart the daemon through its login agent, after the caller has replied.

    Runs on its own thread: daemon.stop() first (stops and joins maintenance and
    wakes run() so it leaves SingleWriterLock cleanly), then
    agents.restart_agent(platform). The thread is non-daemon so the interpreter
    waits for the restart call even once run() has returned and main() is
    exiting. Never uses Daemon._restart_process: that records a watchdog exit
    and spends the 3-per-6-hours budget meant for real stalls.

    Returns the thread (tests join it).
    """
    platform = platform or sys.platform
    if restart is None:
        from mcpbrain import agents
        restart = agents.restart_agent

    def _work():
        try:
            daemon.stop()
        except Exception:  # noqa: BLE001 — still restart; the agent replaces us
            log.exception("restart: daemon.stop() failed; restarting anyway")
        try:
            restart(platform)
        except Exception:  # noqa: BLE001
            log.exception("restart: restart_agent(%s) failed", platform)

    t = threading.Thread(target=_work, name="mcpbrain-restart", daemon=False)
    t.start()
    return t


# -- start at login ------------------------------------------------------------

def set_start_at_login(home, on: bool, *, platform: str | None = None, run=None,
                       plist_path=None) -> dict:
    """Record the owner's "Start when I log in" choice and apply it to the agent.

    The only writer of the `start_at_login` config key. Rewrites the daemon's
    login agent (agents.set_login_start) without reloading it, so the running
    daemon is untouched and the change applies at next login.
    """
    from mcpbrain import agents, config
    on = bool(on)
    config.write_config(str(home), {"start_at_login": on})
    agents.set_login_start(platform or sys.platform, home=str(home), on=on, run=run,
                           plist_path=plist_path)
    return {"ok": True, "applies": "next_login"}


def system_info(daemon, home) -> dict:
    """GET /api/system: login choice, search model, version, watchdog restarts."""
    from mcpbrain import config
    try:
        exits = len(daemon._recent_watchdog_exits())
    except Exception:  # noqa: BLE001 — a status read never fails the page
        exits = 0
    return {"start_at_login": config.start_at_login(str(home)),
            "model": daemon.model_status(),
            "version": _version(),
            "watchdog_exits": exits}


# -- updates -------------------------------------------------------------------

class UpToDate(Exception):
    """apply_update found no newer version to install."""


def check_update() -> dict:
    """Ask the update index for the newest version (update.py's 30 s timeout).

    An unreachable index, or no update channel configured, reads as
    latest=None, newer=False.
    """
    from mcpbrain import update
    installed = update._installed_version()
    idx = update._index_url()
    latest = update._latest_version(idx) if idx else None
    return {"installed": installed, "latest": latest,
            "newer": update._should_update(installed, latest)}


def apply_update(daemon, *, check=None) -> None:
    """Queue an install of the newest version on the daemon's own update path.

    Sets daemon._pending_update (the same flag the auto-update check sets) and
    wakes the loop; run() then stops maintenance, releases its lock and calls
    update.update_from_index, which installs and restarts. Raises UpToDate when
    there is nothing newer.
    """
    info = (check or check_update)()
    if not info.get("newer") or not info.get("latest"):
        raise UpToDate(info)
    daemon._pending_update = info["latest"]
    wake = getattr(daemon, "_wake", None)
    if wake is not None:
        wake.set()


# -- diagnostics ---------------------------------------------------------------

def _line_status(text: str) -> str:
    if text.startswith("✅"):
        return "ok"
    if text.startswith("⚠️") or text.startswith("⚠"):
        return "warn"
    if text.startswith("❌"):
        return "bad"
    return "info"


def parse_doctor(text: str) -> list[dict]:
    return [{"status": _line_status(ln), "text": ln}
            for ln in (raw.rstrip() for raw in (text or "").splitlines()) if ln.strip()]


class Diagnostics:
    """One background `doctor` run at a time, with its last result kept.

    doctor reads the store's integrity, probes the network and shells out, so
    it runs off the request thread. repairs={} keeps it read-only (the default
    repairs kickstart the daemon itself and call its own HTTP API), and the
    caller passes daemon_status so doctor does not call /api/status on us.
    """

    def __init__(self, run_doctor=None):
        self._run_doctor = run_doctor
        self._lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._result = {"running": False, "ran_at": None, "code": None, "lines": []}

    def start(self, home, daemon_status) -> bool:
        """Start a run. False when one is already running."""
        with self._lock:
            if self._result["running"]:
                return False
            self._result = {**self._result, "running": True}
            t = threading.Thread(target=self._work, args=(str(home), daemon_status),
                                 name="mcpbrain-diagnostics", daemon=True)
            self._thread = t
        t.start()
        return True

    def _work(self, home, daemon_status):
        run = self._run_doctor
        if run is None:
            from mcpbrain import doctor
            run = doctor.run_doctor
        try:
            code, text = run(home, repairs={}, daemon_status=daemon_status)
            lines = parse_doctor(text)
        except Exception as exc:  # noqa: BLE001 — report it, don't lose the thread silently
            log.exception("diagnostics run failed")
            code, lines = None, [{"status": "bad", "text": f"Diagnostics failed: {exc}"}]
        with self._lock:
            self._result = {"running": False,
                            "ran_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                            "code": code, "lines": lines}

    def result(self) -> dict:
        with self._lock:
            r = dict(self._result)
            r["lines"] = list(r["lines"])
            return r

    def join(self, timeout: float | None = None) -> None:
        t = self._thread
        if t is not None:
            t.join(timeout)


# -- logs ----------------------------------------------------------------------

def _tail_file(path: Path, max_bytes: int) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if size > max_bytes:
                f.seek(size - max_bytes)
            data = f.read(max_bytes)
    except OSError:
        return ""
    if size > max_bytes:
        # Started mid-file: drop the partial first line.
        nl = data.find(b"\n")
        data = data[nl + 1:] if nl >= 0 else b""
    return data.decode("utf-8", "replace")


def tail_log(home, name: str, max_bytes: int = 65536) -> str:
    """The end of the daemon's or tray's log, at most max_bytes, whole lines.

    name is "daemon" or "tray" (anything else raises ValueError, so a request
    path can never name another file). When both the stdout and stderr files
    have content, each gets a header and half the budget.
    """
    if name not in _LOG_FILES:
        raise ValueError(f"unknown log {name!r}")
    files = [Path(home) / fn for fn in _LOG_FILES[name]]
    files = [p for p in files if p.is_file() and p.stat().st_size > 0]
    if not files:
        return ""
    if len(files) == 1:
        return _tail_file(files[0], max_bytes)
    share = max_bytes // len(files)
    return "".join(f"==> {p.name} <==\n{_tail_file(p, share)}" for p in files)


# -- support report ------------------------------------------------------------

_HEALTH_KEYS = ("version", "paused", "is_configured", "chunk_count", "enriched_count",
                "google_connected", "open_findings", "watchdog_exits",
                "watchdog_limit_reached", "stalled", "backup_in_progress",
                "restore_pending", "restore_error", "google_unavailable")

_HOME_PATTERNS = (
    re.compile(r"/Users/[^/\s'\"]+"),
    re.compile(r"/home/[^/\s'\"]+"),
    re.compile(r"[A-Za-z]:[\\/]+Users[\\/]+[^\\/\s'\"]+", re.IGNORECASE),
)


def _owner_terms(home, daemon_status) -> tuple[list[str], list[str]]:
    """(emails, names) that identify the owner, from config and status."""
    from mcpbrain import config
    st = daemon_status or {}
    emails = {config.owner_email(home).strip().lower(),
              str(st.get("google_account") or "").strip().lower()}
    try:
        emails.add((Path(home) / "google_account").read_text().strip().lower())
    except (OSError, ValueError):
        pass
    names = set(config.owner_aliases(home))
    for full in (config.owner_full_name(home), str(st.get("google_name") or "")):
        full = full.strip().lower()
        if full:
            names.add(full)
            names.update(tok for tok in full.split() if len(tok) >= 3)
    by_len = lambda s: sorted((x for x in s if x), key=len, reverse=True)  # noqa: E731
    return by_len(emails), by_len(names)


def redact(text: str, home, daemon_status=None) -> str:
    """Remove the owner's email addresses, names and aliases, and home paths."""
    # Paths first: a home path often contains the owner's name.
    home_paths = {str(home), str(Path(home).resolve())}
    for p in sorted(home_paths, key=len, reverse=True):
        if len(p) > 1:
            text = text.replace(p, "<mcpbrain home>")
    user_home = str(Path.home())
    if len(user_home) > 1:
        text = text.replace(user_home, "~")
    for pat in _HOME_PATTERNS:
        text = pat.sub("~", text)
    emails, names = _owner_terms(home, daemon_status)
    for e in emails:
        text = re.sub(re.escape(e), "<owner email>", text, flags=re.IGNORECASE)
    for n in names:
        pat = r"(?<!\w)" + r"\s+".join(re.escape(p) for p in n.split()) + r"(?!\w)"
        text = re.sub(pat, "<owner>", text, flags=re.IGNORECASE)
    return text


def support_report(home, daemon_status, *, diagnostics: Diagnostics | None = None) -> str:
    """A plain-text report to paste into a support request, redacted.

    Version, platform, health from daemon_status, the last diagnostics run (if
    any) and the last 200 daemon log lines. The owner's email addresses,
    names and aliases (from config) and home-directory paths are removed.
    """
    import platform as _platform
    st = daemon_status or {}
    out = ["mcpbrain support report",
           f"Generated: {datetime.now(timezone.utc).isoformat(timespec='seconds')}",
           f"Version: {_version()}",
           f"Platform: {sys.platform} {_platform.machine()} · Python {_platform.python_version()}",
           "", "== Health =="]
    for k in _HEALTH_KEYS:
        if k in st:
            out.append(f"{k}: {json.dumps(st[k], default=str)}")
    for name, c in sorted((st.get("connections") or {}).items()):
        if isinstance(c, dict):
            out.append(f"connection {name}: {c.get('state', '?')} {c.get('detail', '')}".rstrip())
    out += ["", "== Diagnostics =="]
    res = diagnostics.result() if diagnostics is not None else None
    if res and res.get("ran_at"):
        out.append(f"Ran at {res['ran_at']} (exit {res['code']})")
        out += [ln["text"] for ln in res["lines"]]
    elif res and res.get("running"):
        out.append("Running now; not finished.")
    else:
        out.append("Not run since mcpbrain started.")
    log_lines = tail_log(home, "daemon", max_bytes=262144).splitlines()[-REPORT_LOG_LINES:]
    out += ["", f"== Daemon log (last {REPORT_LOG_LINES} lines) =="]
    out += log_lines or ["(empty)"]
    return redact("\n".join(out) + "\n", home, st)
