#!/usr/bin/env python3
"""Re-apply prepare.should_enrich to the existing corpus after a gate change.

ATTENDED ONLY. Dry-run by default; --yes is required to write. Stop the daemon
first (single-writer invariant) and confirm a recent verified backup. Nothing in
the daemon's cadences calls this — same posture as bin/consolidate.py.

Cold-marking is REVERSIBLE and is not deletion: a cold chunk stays embedded, in
FTS, and in recall (recall_excludes_cold is off). Undo with
store.set_enrich_state(doc_ids, "").
"""
import argparse
import sys

from mcpbrain import config
from mcpbrain.prepare import should_enrich
from mcpbrain.store import Store

_BATCH = 500


def scan(store) -> list[str]:
    """doc_ids of non-cold chunks that no longer pass the salience gate."""
    return [c["doc_id"] for c in store.iter_hot_chunks() if not should_enrich(c)]


def apply(store, doc_ids: list[str]) -> int:
    """Cold-mark doc_ids in batches. Returns the number marked."""
    if not doc_ids:
        return 0
    for i in range(0, len(doc_ids), _BATCH):
        store.set_enrich_state(doc_ids[i:i + _BATCH], "cold")
    return len(doc_ids)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true",
                    help="actually write; without it this is a dry run")
    args = ap.parse_args(argv)

    store = Store(str(config.app_dir()))
    doc_ids = scan(store)
    print(f"[resalience] {len(doc_ids)} non-cold chunk(s) now fail the gate")
    if not doc_ids:
        return 0
    if not args.yes:
        print("[resalience] DRY RUN — re-run with --yes to cold-mark them")
        print(f"[resalience] first 10: {doc_ids[:10]}")
        return 0
    n = apply(store, doc_ids)
    print(f"[resalience] cold-marked {n} chunk(s)")
    print("[resalience] reverse with store.set_enrich_state(doc_ids, '')")
    print("[resalience] now run: uv run python tests/eval/run_eval.py --gold --k 10")
    print("[resalience] floor: recall@10 >= 0.780 / MRR >= 0.550")
    return 0


if __name__ == "__main__":
    sys.exit(main())
