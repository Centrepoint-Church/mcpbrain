"""Records-hygiene cadences, ported into the product so they run on every OS.

`mcpbrain records-prune`  — drop hot.md entries older than N days, then commit.
`mcpbrain records-health` — read-only checks; exit 1 if any warning.

Both operate on config.records_dir(home); no shell, no records-repo scripts.
"""
from __future__ import annotations

import argparse
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from mcpbrain import config, records_write

_PRUNE_DAYS = 14
_WARN_MEMORY_LINES = 180
_WARN_HOT_DAYS = 14
_WARN_BRAIN_MEMORY_DAYS = 7

# Match dated bullet: "- **2026-05-22: Title.**" or "- 2026-05-14: ..." or "  - **2026-05-12 (session)**:"
_DATED_BULLET = re.compile(
    r"^\s*[-*]\s+(?:\*\*\s*)?(\d{4}-\d{2}-\d{2})",
)



def prune_hot_md(repo: str, *, days: int = _PRUNE_DAYS, now=None) -> int:
    """Drop hot.md dated-bullet lines older than `days`. Returns count removed.

    Idempotent. Does not commit — the subcommand layer commits.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).date()
    p = Path(repo) / "state" / "hot.md"
    if not p.exists():
        return 0
    lines = p.read_text().splitlines(keepends=True)
    out: list[str] = []
    removed = 0
    for line in lines:
        m = _DATED_BULLET.match(line)
        if m:
            try:
                d = datetime.strptime(m.group(1), "%Y-%m-%d").date()
            except ValueError:
                out.append(line)
                continue
            if d < cutoff:
                removed += 1
                continue
        out.append(line)
    if removed:
        p.write_text("".join(out))
    return removed


def context_health(repo: str, mcpbrain_home: str) -> list[str]:
    """Read-only health checks. Returns list of WARN strings (empty == healthy).

    Three checks:
    1. MEMORY.md line count (warn >180)
    2. hot.md stale entries (>14 days)
    3. <mcpbrain_home>/context/memory.md age (>7 days)
    """
    warnings: list[str] = []

    memory_md = Path(repo) / "MEMORY.md"
    if memory_md.exists():
        n = len(memory_md.read_text().splitlines())
        if n > _WARN_MEMORY_LINES:
            warnings.append(
                f"WARN: MEMORY.md has {n} lines (truncation limit 200 — prune old entries)"
            )

    hot_md = Path(repo) / "state" / "hot.md"
    if hot_md.exists():
        cutoff = date.today() - timedelta(days=_WARN_HOT_DAYS)
        for line in hot_md.read_text().splitlines():
            m = _DATED_BULLET.match(line)
            if m:
                try:
                    entry_date = date.fromisoformat(m.group(1))
                except ValueError:
                    continue
                if entry_date < cutoff:
                    warnings.append(
                        f"WARN: hot.md entry {m.group(1)} is >{_WARN_HOT_DAYS} days old"
                        " (run records-prune)"
                    )

    brain_memory = Path(mcpbrain_home) / "context" / "memory.md"
    if brain_memory.parent.exists():
        if not brain_memory.exists():
            warnings.append(
                f"WARN: {brain_memory} missing — mcpbrain daemon may not have run"
            )
        else:
            age = (date.today() - date.fromtimestamp(brain_memory.stat().st_mtime)).days
            if age > _WARN_BRAIN_MEMORY_DAYS:
                warnings.append(
                    f"WARN: {brain_memory} not updated in {age} days — check daemon is running"
                )

    return warnings


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    ap = argparse.ArgumentParser(prog="mcpbrain records-cadence")
    ap.add_argument("cmd", choices=["records-prune", "records-health"])
    ap.add_argument("--days", type=int, default=_PRUNE_DAYS)
    ns = ap.parse_args(argv)
    home = str(config.app_dir())
    repo = config.records_dir(home)
    if ns.cmd == "records-prune":
        n = prune_hot_md(repo, days=ns.days)
        if n:
            records_write._commit_file(repo, "state/hot.md", "prune: hot.md")
        print(f"pruned {n} entries")
        return 0
    warnings = context_health(repo, home)
    for w in warnings:
        print(w, file=sys.stderr)
    return 1 if warnings else 0
