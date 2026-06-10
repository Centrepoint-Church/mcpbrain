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


# ---------------------------------------------------------------------------
# Block-based parsing helpers (ported from ~/joshbrain/bin/prune_hot_md.py)
# ---------------------------------------------------------------------------

def _parse_blocks(text: str) -> list[str]:
    """Split content into blocks.

    Blocks are separated by blank lines.  Additionally, a new dated bullet
    line always starts a new block even when no blank line precedes it —
    continuation lines (indented / non-bullet non-header lines) stay attached
    to the preceding dated bullet as part of its block.

    Each blank line produces an empty-string sentinel ("") in the list so the
    caller can collapse/strip them.
    """
    blocks: list[str] = []
    current: list[str] = []

    def _is_dated_bullet(line: str) -> bool:
        return bool(_DATED_BULLET.match(line))

    def _flush() -> None:
        if current:
            blocks.append("\n".join(current))
            current.clear()

    for line in text.splitlines():
        if line.strip() == "":
            _flush()
            blocks.append("")  # preserve blank separator
        elif _is_dated_bullet(line) and current:
            # New dated bullet — always start a fresh block without requiring
            # an explicit blank-line separator.
            _flush()
            current.append(line)
        else:
            current.append(line)
    _flush()
    return blocks


def _block_date(block: str) -> date | None:
    """Return the date in the leading bullet of a block, or None."""
    if not block:
        return None
    first_line = block.splitlines()[0]
    m = _DATED_BULLET.match(first_line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), "%Y-%m-%d").date()
    except ValueError:
        return None


def _prune_blocks(
    text: str, cutoff: date
) -> tuple[str, list[tuple[date, str]]]:
    """Return (pruned_text, dropped_list).

    Drops every block whose leading bullet date < cutoff.
    Collapses consecutive blank blocks to one; strips leading/trailing blanks.
    Returned text always ends with a newline.
    """
    blocks = _parse_blocks(text)
    kept: list[str] = []
    dropped: list[tuple[date, str]] = []
    for block in blocks:
        d = _block_date(block)
        if d is not None and d < cutoff:
            snippet = block.splitlines()[0][:120]
            dropped.append((d, snippet))
            continue
        kept.append(block)
    # Collapse consecutive blank blocks to one
    cleaned: list[str] = []
    prev_blank = False
    for b in kept:
        is_blank = b == ""
        if is_blank and prev_blank:
            continue
        cleaned.append(b)
        prev_blank = is_blank
    # Strip leading/trailing blank blocks
    while cleaned and cleaned[0] == "":
        cleaned.pop(0)
    while cleaned and cleaned[-1] == "":
        cleaned.pop()
    return "\n".join(cleaned) + "\n", dropped


def _write_prune_log(dropped: list[tuple[date, str]]) -> None:
    """Append dropped entries to <app_dir>/logs/records_prune.log."""
    if not dropped:
        return
    log_path = config.app_dir() / "logs" / "records_prune.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with log_path.open("a") as f:
        f.write(f"\n=== {now} pruned {len(dropped)} entries ===\n")
        for d, snippet in dropped:
            f.write(f"  [{d}] {snippet}\n")


def prune_hot_md(
    repo: str, *, days: int = _PRUNE_DAYS, now=None, dry_run: bool = False
) -> int:
    """Drop hot.md dated-bullet *blocks* older than `days`. Returns count removed.

    Uses block-based parsing: a dated bullet plus its non-blank continuation
    lines form one block, dropped or kept as a unit.  Consecutive blank lines
    are collapsed; leading/trailing blanks are stripped.

    When dry_run=True the count is computed but the file is NOT written and
    nothing is logged.

    Idempotent. Does not commit — the subcommand layer commits.
    """
    now = now or datetime.now(timezone.utc)
    cutoff = (now - timedelta(days=days)).date()
    p = Path(repo) / "state" / "hot.md"
    if not p.exists():
        return 0
    text = p.read_text()
    pruned, dropped = _prune_blocks(text, cutoff)
    removed = len(dropped)
    if removed and not dry_run:
        p.write_text(pruned)
        _write_prune_log(dropped)
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
    ap.add_argument("--dry-run", action="store_true",
                    help="Show what would be pruned without writing")
    ns = ap.parse_args(argv)
    home = str(config.app_dir())
    repo = config.records_dir(home)
    if ns.cmd == "records-prune":
        n = prune_hot_md(repo, days=ns.days, dry_run=ns.dry_run)
        if n and not ns.dry_run:
            records_write._commit_file(repo, "state/hot.md", "prune: hot.md")
        print(f"pruned {n} entries")
        return 0
    warnings = context_health(repo, home)
    for w in warnings:
        print(w, file=sys.stderr)
    return 1 if warnings else 0
