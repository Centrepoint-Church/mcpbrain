"""mcpbrain monitor — reads local state only and reports daemon/enrichment health.

Exit 0 = healthy; exit 1 = one or more problems (daemon down, sync error,
enrichment idle, backup stale). Reuses probes.all_connections so the CLI and the
wizard never disagree.
"""
from __future__ import annotations

import logging
from pathlib import Path

log = logging.getLogger(__name__)
_FAIL = "needs_action"
_MONITORED = {
    "claude":     "Daemon down — MCP server not seen recently",
    "enrichment": "Enrichment idle — run the backfill skill in Cowork",
    "backup":     "Backup stale — snapshot is overdue",
}


def _has_recent_error_log(home: str) -> bool:
    p = Path(home) / "logs" / "error.log"
    try:
        return p.exists() and p.stat().st_size > 0
    except OSError:
        return False


def run_monitor(home: str) -> tuple[int, str]:
    from mcpbrain import probes
    try:
        conns = probes.all_connections(home, store=None)
    except Exception as exc:  # noqa: BLE001
        return 1, f"monitor: could not read probes: {exc}"
    problems: list[str] = []
    if _has_recent_error_log(home):
        problems.append("sync error — check logs/error.log")
    for key, message in _MONITORED.items():
        if conns.get(key, {}).get("state") == _FAIL:
            problems.append(message)
    return (1, "; ".join(problems)) if problems else (0, "ok")


def main(argv=None) -> None:
    import sys
    from mcpbrain import config
    code, msg = run_monitor(str(config.app_dir()))
    print(msg)
    sys.exit(code)
