#!/usr/bin/env python3
"""Attended reflow control.

  python bin/reflow.py status          show progress and whether reflow is halted
  python bin/reflow.py resume --yes    clear a halt AFTER investigating it

A halt means apply_reflow found a dangling reference and rolled back. Read the
daemon log and the failing item's last_error before resuming.

`status` NEVER writes and never creates anything: it opens the store
READ-ONLY and does not call store.init(). The store this points at is
normally the LIVE, daemon-owned store -- this repo had a real corruption
incident (2026-09-10, see CLAUDE.md) from an attended script writing to the
store concurrently with the daemon. `resume --yes` performs exactly one
write (clearing the halt cursor) and also never calls init().
"""
import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain import config             # noqa: E402
from mcpbrain.embed import get_embedder  # noqa: E402
from mcpbrain.store import Store        # noqa: E402


def _describe_store_error(exc: sqlite3.OperationalError, db_path) -> str:
    """Only a missing table means the daemon never initialised this store; a
    busy/locked store (or anything else) is reported as what it is."""
    msg = str(exc)
    if "no such table" in msg:
        return (f"reflow tables not present in {db_path} ({msg}) -- this store "
                f"has never been initialized by the daemon")
    return f"could not read the store at {db_path}: {msg} (is the daemon mid-write? retry)"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reflow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("resume")
    r.add_argument("--yes", action="store_true")
    ns = ap.parse_args(argv)

    db_path = config.store_path()
    if not db_path.exists():
        print(f"no store at {db_path}", file=sys.stderr)
        return 2

    # Dim comes from the embedder, exactly as bin/repair.py/bin/consolidate.py
    # do it -- there is no config.embed_dim; the org pin's `dim` is a
    # fleet-baseline field, not this install's live dimension.
    dim = get_embedder("bge-small").dim
    store = Store(db_path, dim=dim, read_only=(ns.cmd == "status"))
    try:
        halted = store.get_cursor("reflow:halted") or ""
        if ns.cmd == "status":
            # live_remaining: the owners still to do, counted now (a bounded,
            # read-only selector query) rather than the seed's last figure.
            print(store.reflow_status(live_remaining=True))
            return 0
    except sqlite3.OperationalError as exc:
        print(_describe_store_error(exc, db_path), file=sys.stderr)
        return 2

    if not halted:
        print("reflow is not halted")
        return 0
    if not ns.yes:
        print(f"halted: {halted}\nre-run with --yes to clear")
        return 1
    try:
        store.set_cursor("reflow:halted", "")
    except sqlite3.OperationalError as exc:
        print(_describe_store_error(exc, db_path), file=sys.stderr)
        return 2
    print("halt cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
