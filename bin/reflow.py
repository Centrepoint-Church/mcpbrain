#!/usr/bin/env python3
"""Attended reflow control.

  python bin/reflow.py status          show progress and whether reflow is halted
  python bin/reflow.py resume --yes    clear a halt AFTER investigating it

A halt means apply_reflow found a dangling reference and rolled back. Read the
daemon log and the failing item's last_error before resuming.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain import config            # noqa: E402
from mcpbrain.embed import get_embedder  # noqa: E402
from mcpbrain.store import Store       # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="reflow")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status")
    r = sub.add_parser("resume")
    r.add_argument("--yes", action="store_true")
    ns = ap.parse_args(argv)
    # Dim comes from the embedder, exactly as bin/repair.py does it -- there
    # is no config.embed_dim; the org pin's `dim` is a fleet-baseline field,
    # not this install's live dimension.
    store = Store(config.store_path(), dim=get_embedder("bge-small").dim)
    store.init()
    halted = store.get_cursor("reflow:halted") or ""
    if ns.cmd == "status":
        print({**store.reflow_stats(), "halted": halted or None,
               "integrity": store.get_cursor("reflow:integrity_checked")})
        return 0
    if not halted:
        print("reflow is not halted")
        return 0
    if not ns.yes:
        print(f"halted: {halted}\nre-run with --yes to clear")
        return 1
    store.set_cursor("reflow:halted", "")
    print("halt cleared")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
