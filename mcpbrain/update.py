"""mcpbrain update — reinstall from the wheel index, then restart.

Resolves the index URL (env → config → default), asks it for the newest
mcpbrain wheel, and if we're behind reinstalls via uv (the index is marked
explicit, so deps still come from PyPI), then restarts the daemon + tray.
"""
import os
import re
import shutil
import subprocess
import sys
import urllib.request
from importlib.metadata import version, PackageNotFoundError
from pathlib import Path

from packaging.version import Version, InvalidVersion

_WHEEL_RE = re.compile(r"mcpbrain-([^-]+)-py3")


def _index_url() -> str | None:
    """The wheel index to update from: env, else config, else the tenant profile.

    None when none resolves — a build carrying no tenant profile does not
    auto-update. It must never fall back to another organisation's index.
    """
    env = os.environ.get("MCPBRAIN_INDEX_URL")
    if env:
        return env
    try:
        from mcpbrain.config import read_config, app_dir
        cfg = read_config(str(app_dir()))
        if cfg.get("update_index_url"):
            return cfg["update_index_url"]
    except Exception:  # noqa: BLE001 — config read must never break update
        pass
    from mcpbrain import tenant
    prof = tenant.profile()
    return prof.index_url if prof else None


def _fetch(url: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def _parse(v: str) -> Version:
    try:
        return Version(v)
    except InvalidVersion:
        return Version("0")


def _installed_version() -> str:
    try:
        return version("mcpbrain")
    except PackageNotFoundError:
        return "0.0.0"


def _latest_version(index_url: str) -> str | None:
    """Newest mcpbrain version on the PEP 503 index, or None if unreachable."""
    try:
        html = _fetch(index_url.rstrip("/") + "/mcpbrain/")
    except Exception:  # noqa: BLE001 — offline / index down: no update
        return None
    versions = _WHEEL_RE.findall(html)
    if not versions:
        return None
    return str(max(versions, key=_parse))


def _should_update(installed: str, latest: str | None) -> bool:
    return bool(latest) and _parse(latest) > _parse(installed)


def _resolve_uv() -> str:
    """Return the uv binary to use: PATH → ~/.local/bin/uv[.exe] → bare 'uv'."""
    found = shutil.which("uv")
    if found:
        return found
    suffix = ".exe" if os.name == "nt" else ""
    candidate = Path.home() / ".local" / "bin" / f"uv{suffix}"
    if candidate.exists():
        return str(candidate)
    return "uv"


def _run(cmd: list) -> tuple[str, int]:
    kwargs: dict = {"stdout": subprocess.PIPE, "stderr": subprocess.STDOUT, "text": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
    result = subprocess.run(cmd, **kwargs)
    return result.stdout or "", result.returncode


def _restart_agent() -> None:
    from mcpbrain import agents
    agents.restart_agent(sys.platform)


def update_from_index(index_url: str) -> int:
    """Reinstall mcpbrain from the index via uv, then restart. Returns 0 on success."""
    out, rc = _run([
        _resolve_uv(), "tool", "install",
        # Pin the interpreter: mcpbrain requires Python >=3.12 and uv otherwise
        # resolves the tool env against the machine's default Python (often 3.9
        # on macOS / 3.11 on Windows), which fails the requires-python solve.
        # uv fetches a managed 3.12 if none is present. (Verified: install fails
        # without this on a machine whose default Python is <3.12.)
        "--python", "3.12",
        "--index", f"mcpbrain={index_url}",
        # Install spec keeps the [daemon] extra permanently: on a pre-0.7.119
        # wheel it still pulls fastembed (extra-only there), and on every wheel
        # after, `daemon = []` is a declared-but-empty alias (fastembed moved to
        # base deps) that uv resolves silently — so this one command line is
        # correct against whatever version the index currently serves.
        # --reinstall-package still takes the bare package name — extras aren't
        # a separate installed package.
        "mcpbrain[daemon]", "--upgrade", "--reinstall-package", "mcpbrain",
    ])
    if rc != 0:
        print("Update failed (uv tool install):\n" + out.strip(), file=sys.stderr)
        return rc
    _restart_agent()
    return 0


def main(argv: list) -> int:
    index_url = _index_url()
    if not index_url:
        print("mcpbrain: no wheel index configured (no tenant profile, no "
              "MCPBRAIN_INDEX_URL, no update_index_url) — skipping update.")
        return 0
    installed = _installed_version()
    latest = _latest_version(index_url)
    if not _should_update(installed, latest):
        print(f"Already up to date (v{installed}).")
        return 0
    print(f"Updating mcpbrain {installed} → {latest} …")
    return update_from_index(index_url)
