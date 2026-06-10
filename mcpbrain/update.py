"""mcpbrain update — reinstall from the wheel index, then restart.

Resolves the index URL (env → config → default), asks it for the newest
mcpbrain wheel, and if we're behind reinstalls via uv (the index is marked
explicit, so deps still come from PyPI), then restarts the daemon + tray.
"""
import os
import re
import subprocess
import sys
import urllib.request
from importlib.metadata import version, PackageNotFoundError

from packaging.version import Version, InvalidVersion

# Maintainer sets this to the published Pages index (the dist repo's /simple/).
DEFAULT_INDEX_URL = "https://CHANGE-ME.github.io/mcpbrain-dist/simple/"

_WHEEL_RE = re.compile(r"mcpbrain-([^-]+)-py3")


def _index_url() -> str:
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
    return DEFAULT_INDEX_URL


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


def _run(cmd: list) -> tuple[str, int]:
    result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
    return result.stdout or "", result.returncode


def _restart_agent() -> None:
    from mcpbrain import agents
    agents.restart_agent(sys.platform)


def update_from_index(index_url: str) -> int:
    """Reinstall mcpbrain from the index via uv, then restart. Returns 0 on success."""
    out, rc = _run([
        "uv", "tool", "install",
        "--index", f"mcpbrain={index_url}",
        "mcpbrain", "--upgrade", "--reinstall-package", "mcpbrain",
    ])
    if rc != 0:
        print("Update failed (uv tool install):\n" + out.strip(), file=sys.stderr)
        return rc
    _restart_agent()
    return 0


def main(argv: list) -> int:
    index_url = _index_url()
    if "CHANGE-ME" in index_url:
        print("Update channel not configured (index URL is the placeholder). "
              "See docs/DISTRIBUTION.md.", file=sys.stderr)
        return 1
    installed = _installed_version()
    latest = _latest_version(index_url)
    if not _should_update(installed, latest):
        print(f"Already up to date (v{installed}).")
        return 0
    print(f"Updating mcpbrain {installed} → {latest} …")
    return update_from_index(index_url)
