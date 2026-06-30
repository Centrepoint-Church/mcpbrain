"""One-shot Q1 salience-gate backfill.

The salience gate (`prepare.should_enrich`) normally evaluates chunks only as
the daemon pulls them into a prep cycle, capped per cycle. On a store with a
large pre-existing backlog that means the gate's reduction lands slowly — one
thread_cap-sized slice at a time. This module applies the *same* gate to the
*entire* unenriched, non-cold backlog in one pass, cold-marking everything the
gate would skip so it never enters the extraction queue.

It changes nothing the steady-state gate wouldn't eventually do itself:
  - same predicate: prepare.should_enrich(chunk)
  - same backlog set: store.unenriched_chunks() (enriched=0 AND not cold)
  - same effect: store.set_enrich_state(doc_ids, "cold")  ← reversible

Reversal: set the chunks' enrich_state back to '' (e.g.
`UPDATE chunks SET enrich_state='' WHERE enrich_state='cold'`) and they
re-enter the backlog. Cold chunks stay embedded/searchable throughout.

Usage:
    # dry run — report what WOULD be gated, write nothing (default):
    python -m mcpbrain.maintenance.salience_backfill
    # apply — cold-mark the gated chunks:
    python -m mcpbrain.maintenance.salience_backfill --apply
"""

import argparse
import json
import logging
from collections import Counter

from mcpbrain import config, prepare
from mcpbrain.store import Store

log = logging.getLogger("mcpbrain.maintenance.salience_backfill")

_WRITE_BATCH = 1000


def _source_of(meta: dict) -> str:
    return str(meta.get("source_type") or meta.get("source") or "unknown").lower()


def run(store, *, apply: bool, require_drive_mention: bool = False) -> dict:
    """Evaluate the whole unenriched, non-cold backlog through should_enrich().

    Returns a summary with per-source kept/gated counts. Only writes (cold-marks
    the gated chunks) when apply=True; otherwise it is a pure dry run.
    """
    backlog = store.unenriched_chunks()  # enriched=0 AND enrich_state != 'cold'
    kept_by_src: Counter = Counter()
    gated_by_src: Counter = Counter()
    to_cold: list[str] = []

    for chunk in backlog:
        meta = chunk.get("metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}
        src = _source_of(meta)
        keep = prepare.should_enrich(chunk)
        if keep and require_drive_mention and prepare._is_drive_chunk(meta):
            keep = prepare._drive_mentioned_in_email(store, meta)
        if keep:
            kept_by_src[src] += 1
        else:
            gated_by_src[src] += 1
            to_cold.append(chunk["doc_id"])

    if apply and to_cold:
        for i in range(0, len(to_cold), _WRITE_BATCH):
            store.set_enrich_state(to_cold[i:i + _WRITE_BATCH], "cold")

    return {
        "scanned": len(backlog),
        "would_gate": len(to_cold),
        "would_keep": len(backlog) - len(to_cold),
        "applied": bool(apply),
        "gated_by_source": dict(gated_by_src),
        "kept_by_source": dict(kept_by_src),
        "cold_total_after": store.cold_chunk_count() if apply else None,
    }


def main(argv=None) -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true",
                    help="cold-mark the gated chunks (default: dry run, write nothing)")
    ap.add_argument("--require-drive-mention", action="store_true",
                    help="also require Drive docs to be mentioned in email (stricter; opt-in)")
    args = ap.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    from mcpbrain.embed import get_embedder
    emb = get_embedder("bge-small")
    store = Store(config.store_path(), dim=emb.dim)
    store.init()

    summary = run(store, apply=args.apply,
                  require_drive_mention=args.require_drive_mention)

    mode = "APPLIED" if summary["applied"] else "DRY RUN (no writes)"
    print(f"\n=== Q1 salience-gate backfill — {mode} ===")
    print(f"scanned (unenriched, non-cold) : {summary['scanned']:,}")
    print(f"would keep for extraction      : {summary['would_keep']:,}")
    print(f"would gate to cold             : {summary['would_gate']:,}")
    if summary["scanned"]:
        pct = 100 * summary["would_gate"] / summary["scanned"]
        print(f"gated fraction                 : {pct:.1f}%")
    print("\ngated by source:")
    for src, n in sorted(summary["gated_by_source"].items(), key=lambda kv: -kv[1]):
        print(f"  {src:22} {n:,}")
    print("\nkept by source:")
    for src, n in sorted(summary["kept_by_source"].items(), key=lambda kv: -kv[1]):
        print(f"  {src:22} {n:,}")
    if summary["applied"]:
        print(f"\ncold chunks total after apply  : {summary['cold_total_after']:,}")
    else:
        print("\n(dry run — re-run with --apply to cold-mark the gated chunks)")


if __name__ == "__main__":
    main()
