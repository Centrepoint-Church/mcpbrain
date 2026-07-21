"""Fallback: ensure the x64 VC++ runtime DLLs onnxruntime/sqlite-vec need are on
the daemon's DLL search path, in a location that survives package reinstalls.

PRIMARY fix is the x64 vc_redist installed by install.ps1; this is the last-resort
repair the doctor runs only if the embedder still can't load on Windows."""
from __future__ import annotations

import os
import shutil
from pathlib import Path

_REQUIRED = ("VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "MSVCP140.dll", "MSVCP140_1.dll")


def is_x64_pe(path: str) -> bool:
    """True iff the PE file's IMAGE_FILE_HEADER.Machine is IMAGE_FILE_MACHINE_AMD64 (0x8664)."""
    try:
        with open(path, "rb") as f:
            data = f.read(0x400)
        if data[:2] != b"MZ":
            return False
        e_lfanew = int.from_bytes(data[0x3C:0x40], "little")
        if data[e_lfanew:e_lfanew + 4] != b"PE\x00\x00":
            return False
        machine = int.from_bytes(data[e_lfanew + 4:e_lfanew + 6], "little")
        return machine == 0x8664
    except OSError:
        return False


def _SEARCH_DIRS():  # pragma: no cover — real system dirs, monkeypatched in tests
    root = os.environ.get("SystemRoot", r"C:\Windows")
    return [Path(root) / "System32", Path(root) / "WinSxS"]


def ensure_vcruntime_dlls(app_dir: str) -> list[str]:
    """Copy any missing required x64 runtime DLL from an MS-signed x64 copy on the
    machine into <app_dir>/vcruntime. Returns the names copied. Best-effort."""
    dest = Path(app_dir) / "vcruntime"
    dest.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in _REQUIRED:
        if (dest / name).exists():
            continue
        for d in _SEARCH_DIRS():
            found = _find_x64(d, name)
            if found:
                shutil.copy2(found, dest / name)
                copied.append(name)
                break
    return copied


def _find_x64(root: Path, name: str):  # pragma: no cover — filesystem walk
    if not root.is_dir():
        return None
    direct = root / name
    if direct.is_file() and is_x64_pe(str(direct)):
        return direct
    try:
        for cand in root.rglob(name):
            if cand.is_file() and is_x64_pe(str(cand)):
                return cand
    except OSError:
        pass
    return None
