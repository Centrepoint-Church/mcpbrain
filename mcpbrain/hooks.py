"""Install mcpbrain's SessionStart/SessionEnd hooks into ~/.claude/settings.json.

The hooks call the cross-platform `mcpbrain` console script (on PATH) so they
work on macOS/Windows/Linux without shell scripts. Installation is mergeful and
idempotent: existing keys and other hooks are preserved, a malformed settings
file is refused (never clobbered), and a re-run never duplicates our entry.
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# (event, command) pairs. The marker substring identifies OUR entries on re-run.
_HOOKS = (
    ("SessionStart", "session-start"),
    ("SessionEnd", "session-end"),
)


def settings_path() -> Path:
    base = os.getenv("CLAUDE_CONFIG_DIR")
    return (Path(base) if base else Path.home() / ".claude") / "settings.json"


def _mcpbrain_bin() -> str:
    cand = Path(sys.executable).with_name("mcpbrain")
    if cand.exists():
        return str(cand)
    argv0 = Path(sys.argv[0])
    if argv0.name in ("mcpbrain", "mcpbrain.exe") and argv0.exists():
        return str(argv0)
    return shutil.which("mcpbrain") or "mcpbrain"


def _load(p: Path) -> dict:
    if not p.exists():
        return {}
    raw = p.read_text()
    if not raw.strip():
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"{p} is not valid JSON; refusing to overwrite it: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError(f"{p} is not a JSON object; refusing to overwrite it.")
    return data


def _write(p: Path, data: dict) -> None:
    p.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=p.name + ".", suffix=".tmp")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(json.dumps(data, indent=2) + "\n")
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def install_session_hooks() -> Path:
    """Merge our two command hooks into settings.json. Idempotent. Returns the path."""
    p = settings_path()
    data = _load(p)
    bin_path = _mcpbrain_bin()
    hooks = data.get("hooks")
    if not isinstance(hooks, dict):
        hooks = {}
    for event, marker in _HOOKS:
        blocks = hooks.get(event)
        if not isinstance(blocks, list):
            blocks = []
        present = any(
            marker in (h.get("command") or "")
            for blk in blocks if isinstance(blk, dict)
            for h in blk.get("hooks", []) if isinstance(h, dict)
        )
        if not present:
            blocks.append({"hooks": [{"type": "command", "command": f"{bin_path} {marker}"}]})
        hooks[event] = blocks
    data["hooks"] = hooks
    _write(p, data)
    return p


def uninstall_session_hooks() -> Path:
    p = settings_path()
    data = _load(p)
    hooks = data.get("hooks")
    if isinstance(hooks, dict):
        for event, marker in _HOOKS:
            blocks = hooks.get(event)
            if not isinstance(blocks, list):
                continue
            kept = []
            for blk in blocks:
                if not isinstance(blk, dict):
                    kept.append(blk)
                    continue
                inner = [h for h in blk.get("hooks", [])
                         if not (isinstance(h, dict) and marker in (h.get("command") or ""))]
                if inner:
                    kept.append({**blk, "hooks": inner})
            if kept:
                hooks[event] = kept
            else:
                hooks.pop(event, None)
        data["hooks"] = hooks
        _write(p, data)
    return p


def hooks_status() -> dict:
    """{'installed': bool} — true only when BOTH our hooks are present."""
    try:
        data = _load(settings_path())
    except ValueError:
        return {"installed": False}
    hooks = data.get("hooks") or {}

    def has(event, marker):
        return any(
            marker in (h.get("command") or "")
            for blk in (hooks.get(event) or []) if isinstance(blk, dict)
            for h in blk.get("hooks", []) if isinstance(h, dict)
        )

    return {"installed": all(has(e, m) for e, m in _HOOKS)}
