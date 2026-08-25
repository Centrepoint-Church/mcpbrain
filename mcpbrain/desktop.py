"""Quit + relaunch Claude Desktop so it reloads its MCP config (the brain_*
connector setup wrote). Claude Desktop only reads mcpServers at launch and
overwrites the config while running, so a reload is the only way to connect."""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import time
from pathlib import Path


def _windows_claude_exe() -> str | None:
    base = os.environ.get("LOCALAPPDATA")
    if base:
        p = Path(base) / "Programs" / "claude" / "Claude.exe"
        if p.is_file():
            return str(p)
    return shutil.which("Claude")


_MANUAL = "restart Claude Desktop manually to load the brain"

# How long to wait for Claude Desktop to actually exit before writing the config.
# The app rewrites its own config on the way out, so writing too early loses the
# entry. Bounded: a hung quit must not strand the user with the app closed.
_EXIT_WAIT_S = 10.0
_EXIT_POLL_S = 0.25


def _claude_running() -> bool:  # pragma: no cover — touches the process table
    if sys.platform == "win32":
        r = subprocess.run(["tasklist", "/FI", "IMAGENAME eq Claude.exe"],
                           capture_output=True, text=True, check=False)
        return "Claude.exe" in (r.stdout or "")
    r = subprocess.run(["pgrep", "-x", "Claude"], capture_output=True, check=False)
    return r.returncode == 0


def quit_claude_desktop() -> dict:
    """Ask Claude Desktop to quit and wait (bounded) for it to actually exit."""
    if sys.platform == "win32":
        subprocess.run(["taskkill", "/IM", "Claude.exe", "/F"],
                       capture_output=True, check=False)
    elif sys.platform == "darwin":
        subprocess.run(["osascript", "-e", 'quit app "Claude"'],
                       capture_output=True, check=False)
    else:
        return {"quit": False, "detail": f"auto-restart unsupported here; {_MANUAL}"}
    deadline = time.monotonic() + _EXIT_WAIT_S
    while time.monotonic() < deadline:
        if not _claude_running():
            return {"quit": True, "detail": "Claude Desktop exited"}
        time.sleep(_EXIT_POLL_S)
    # Proceed anyway: a still-running app may clobber the write, but leaving it
    # shut down with nothing relaunching it is strictly worse.
    return {"quit": False, "detail": "Claude Desktop did not exit in time"}


def launch_claude_desktop() -> dict:
    """Start Claude Desktop again."""
    if sys.platform == "win32":
        exe = _windows_claude_exe()
        if not exe:
            return {"launched": False, "detail": f"Claude.exe not found; {_MANUAL}"}
        subprocess.Popen([exe])
        return {"launched": True, "detail": "Claude Desktop is restarting"}
    if sys.platform == "darwin":
        subprocess.run(["open", "-a", "Claude"], capture_output=True, check=False)
        return {"launched": True, "detail": "Claude Desktop is restarting"}
    return {"launched": False, "detail": f"auto-restart unsupported here; {_MANUAL}"}


def relaunch_claude_desktop(on_quit=None) -> dict:
    """Quit Claude Desktop, run ``on_quit`` while it is down, then relaunch.

    The callback is where the connector write belongs: Claude Desktop rewrites its
    config as it exits, so an entry written before the quit is discarded. Running
    it in the gap is the only ordering that reliably survives.

    Never raises, and never leaves the app shut: a callback that blows up is
    reported, and the relaunch happens regardless.
    """
    detail_parts: list[str] = []
    try:
        q = quit_claude_desktop()
        detail_parts.append(q["detail"])
        if on_quit is not None:
            try:
                on_quit()
            except Exception as exc:  # noqa: BLE001 — never strand the app closed
                detail_parts.append(f"connector write failed ({exc})")
        launched = launch_claude_desktop()
        detail_parts.append(launched["detail"])
        return {"relaunched": bool(launched["launched"]),
                "detail": "; ".join(p for p in detail_parts if p)}
    except Exception as exc:  # noqa: BLE001 — never propagate to the control API
        return {"relaunched": False, "detail": f"restart failed ({exc}); {_MANUAL}"}
