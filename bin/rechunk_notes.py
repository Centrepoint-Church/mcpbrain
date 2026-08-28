#!/usr/bin/env python3
"""Re-chunk oversize captured notes so their whole body is embedded.

ATTENDED ONLY. Dry-run by default; --yes required. Stop the daemon first and
confirm a recent verified backup — this rewrites chunk rows.

Notes bypassed chunk_text (drain_captures wrote one row per note), so only the
first ~2,000 chars of each was ever embedded. On the live store that is 1,192
notes holding 21.1MB — 96% of all note text. 2,109 notes already fit one chunk
and are untouched.

GOLD-GATED: run tests/eval/run_eval.py --gold --k 10 before and after.
Floor: recall@10 >= 0.780 / MRR >= 0.550. It should IMPROVE — 21.1MB currently
has no vector past each note's first ~2,000 chars.
"""
import argparse
import sys

from mcpbrain import config
from mcpbrain.chunking import chunk_text, content_hash
from mcpbrain.embed import get_embedder
from mcpbrain.store import Store


def plan(store) -> list[dict]:
    """Notes whose body needs more than one chunk and is not already split."""
    out = []
    for row in store.note_chunks(include_expired=True, limit=10 ** 9):
        meta = row["metadata"]
        if meta.get("chunk_total", 1) > 1:
            continue                       # already chunked
        pieces = chunk_text(row["text"])
        if len(pieces) > 1:
            out.append({"note_id": row["doc_id"], "text": row["text"],
                        "metadata": meta, "pieces": pieces})
    return out


def apply(store, items: list[dict]) -> int:
    """Rewrite each planned note as suffixed chunks. Returns notes rewritten."""
    n = 0
    for it in items:
        base, pieces = it["note_id"], it["pieces"]
        chash = content_hash(it["text"])
        base_meta = {**it["metadata"], "note_id": base}
        for i, piece in enumerate(pieces):
            store.upsert_chunk(f"{base}-{i}", piece, chash,
                               {**base_meta, "chunk_index": i,
                                "chunk_total": len(pieces)})
        store.delete_chunks([base])        # the old whole-body row
        n += 1
    return n


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--yes", action="store_true")
    args = ap.parse_args(argv)
    store = Store(config.store_path(), dim=get_embedder("bge-small").dim)
    items = plan(store)
    chars = sum(len(i["text"]) for i in items)
    print(f"[rechunk] {len(items)} note(s), {chars:,} chars need re-chunking")
    if not items or not args.yes:
        if items:
            print("[rechunk] DRY RUN — re-run with --yes")
        return 0
    print(f"[rechunk] rewrote {apply(store, items)} note(s)")
    print("[rechunk] now run: uv run python tests/eval/run_eval.py --gold --k 10")
    print("[rechunk] floor: recall@10 >= 0.780 / MRR >= 0.550")
    return 0


if __name__ == "__main__":
    sys.exit(main())
