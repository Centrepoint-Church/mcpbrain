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


def relaunch_claude_desktop() -> dict:
    """Best-effort quit + relaunch of Claude Desktop. Never raises.
    Returns {"relaunched": bool, "detail": str}."""
    manual = "restart Claude Desktop manually to load the brain"
    try:
        if sys.platform == "win32":
            subprocess.run(["taskkill", "/IM", "Claude.exe", "/F"],
                           capture_output=True, check=False)
            exe = _windows_claude_exe()
            if not exe:
                return {"relaunched": False, "detail": f"Claude.exe not found; {manual}"}
            time.sleep(1.0)
            subprocess.Popen([exe])
            return {"relaunched": True, "detail": "Claude Desktop is restarting"}
        if sys.platform == "darwin":
            subprocess.run(["osascript", "-e", 'quit app "Claude"'],
                           capture_output=True, check=False)
            time.sleep(1.0)
            subprocess.run(["open", "-a", "Claude"], capture_output=True, check=False)
            return {"relaunched": True, "detail": "Claude Desktop is restarting"}
        return {"relaunched": False, "detail": f"auto-restart unsupported here; {manual}"}
    except Exception as exc:  # noqa: BLE001 — never propagate to the control API
        return {"relaunched": False, "detail": f"restart failed ({exc}); {manual}"}
