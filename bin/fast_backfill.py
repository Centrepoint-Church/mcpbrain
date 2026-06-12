#!/usr/bin/env python3
"""Parallel enrichment backfill CLI.

Tactical one-shot drainer that fans the slow `claude --print` extractor calls out
across N worker threads while keeping every SQLite write on the main thread. Run
with the daemon paused or stopped (the script guards this). Steady-state
enrichment stays on the daemon/cowork path.

Run with:
  python bin/fast_backfill.py                  # ~/.mcpbrain, sonnet, 8 workers
  python bin/fast_backfill.py --workers 12 --model haiku
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Allow running in-place from a checkout without `uv tool install`.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain import config, parallel_backfill   # noqa: E402

DEFAULT_HOME = Path.home() / ".mcpbrain"


def parse_args(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--home", type=Path, default=DEFAULT_HOME)
    ap.add_argument("--model", default="sonnet")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--max-batches", type=int, default=None)
    ap.add_argument("--force", action="store_true",
                    help="bypass the daemon-paused guard (advanced)")
    return ap.parse_args(argv)


def main(argv=None) -> int:
    import logging
    import signal
    import threading
    logging.basicConfig(level=logging.INFO, format="[%(asctime)s] %(message)s",
                        datefmt="%H:%M:%S")
    args = parse_args(argv)
    home = str(args.home.expanduser().resolve())

    import os
    os.environ["MCPBRAIN_HOME"] = home

    status = parallel_backfill.daemon_status(Path(home))
    ok, msg = parallel_backfill.check_daemon_guard(status=status, force=args.force)
    print(msg)
    if not ok:
        return 2
    if status:
        total = status.get("chunk_count", 0)
        enr = status.get("enriched_count", 0)
        print(f"backlog: {enr:,}/{total:,} enriched ({total - enr:,} to go)")

    cancel = threading.Event()

    def _on_signal(_sig, _frame):
        # Note: cancellation is cooperative — in-flight workers run to
        # completion (up to --timeout) and are drained before the loop exits.
        print("\ncancellation requested — finishing in-flight batches, then stopping")
        cancel.set()

    signal.signal(signal.SIGINT, _on_signal)
    signal.signal(signal.SIGTERM, _on_signal)

    from mcpbrain.store import Store
    from mcpbrain.embed import get_embedder
    emb = get_embedder("bge-small")
    store = Store(config.store_path(), dim=emb.dim, read_only=False)

    res = parallel_backfill.run_parallel_backfill(
        store=store, embedder=emb, home=home, model=args.model,
        workers=args.workers, batch_size=args.batch_size, timeout=args.timeout,
        max_batches=args.max_batches, cancel_event=cancel)
    print(f"fast-backfill: {res['status']} after {res['batches']} batches "
          f"({res['threads_dispatched']:,} threads, {res['quarantined']} quarantined)")
    return 0 if res["status"] in ("done", "cancelled", "max_batches") else 1


if __name__ == "__main__":
    sys.exit(main())
