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
from mcpbrain.chunking import NOTE_MAX_CHARS, content_hash, split_lossless
from mcpbrain.embed import get_embedder
from mcpbrain.memory_tier import _IDENTITY_SEED_DOC_ID
from mcpbrain.store import Store

# Notes whose doc_id is referenced verbatim elsewhere and therefore must stay a
# single row. memory_tier.seed_core_identity writes _IDENTITY_SEED_DOC_ID at a
# FIXED id and then calls set_chunk_type / set_chunk_tier / set_chunk_salience
# on that exact id; splitting it into -0/-1 leaves the base id with no row and
# silently breaks all four. It is 2,836 chars live, so the cost of leaving it
# whole is a ~1,000-char tail past the embed window — far cheaper than breaking
# core-tier identity.
_NEVER_SPLIT = frozenset({_IDENTITY_SEED_DOC_ID})


def _is_lossless(text: str, pieces: list[str]) -> bool:
    """True when re-joining `pieces` reproduces `text` exactly.

    Retained as a GUARD, not a gate. It used to reject most of the corpus: the
    sweep split with chunk_text, which is a retrieval chunker and cannot
    round-trip a paragraph larger than the budget (_split_paragraph word-splits
    via para.split(), collapsing internal whitespace, and duplicates the last
    `overlap` words across a boundary). On the live store that skipped 930 of
    the 1,180 oversize notes -- 16,022,678 chars of tail left with no vector --
    and no overlap setting changed it: overlap=0 rescued zero of them.

    split_lossless carries each break's separator on the preceding piece, so ""
    rejoins it byte-for-byte and all 930 now pass. The check stays because this
    sweep DELETES the original whole-body row -- the only copy of the note's
    text -- and store.note_chunks() thereafter serves the reassembly AS the
    note. Verifying is cheap; being wrong is unrecoverable.
    """
    return "".join(pieces) == text


def scan(store) -> tuple[list[dict], int]:
    """(notes to re-chunk, notes skipped because the re-chunk is not lossless)."""
    out, skipped = [], 0
    for row in store.note_chunks(include_expired=True, limit=10 ** 9):
        if row["doc_id"] in _NEVER_SPLIT:
            continue                       # fixed-id note — see _NEVER_SPLIT
        meta = row["metadata"]
        if meta.get("chunk_total", 1) > 1:
            continue                       # already chunked
        pieces = split_lossless(row["text"], NOTE_MAX_CHARS)
        if len(pieces) <= 1:
            continue                       # fits one chunk; nothing to do
        if not _is_lossless(row["text"], pieces):
            # Should now be unreachable (split_lossless is lossless by
            # construction), but left in: a whole-body row with an unembedded
            # tail is strictly better than a rewritten one that lost text.
            skipped += 1
            continue
        out.append({"note_id": row["doc_id"], "text": row["text"],
                    "metadata": meta, "pieces": pieces})
    return out, skipped


def plan(store) -> list[dict]:
    """Notes whose body needs more than one chunk, is not already split, and
    re-chunks losslessly."""
    return scan(store)[0]


def apply(store, items: list[dict]) -> int:
    """Rewrite each planned note as suffixed chunks. Returns notes rewritten."""
    n = 0
    for it in items:
        base, pieces = it["note_id"], it["pieces"]
        if not _is_lossless(it["text"], pieces):
            # Defense in depth: scan() already filters these out, but the delete
            # below is irreversible and this is the only copy of the text.
            print(f"[rechunk] SKIP {base}: re-chunk is not lossless")
            continue
        chash = content_hash(it["text"])
        # "split": "lossless" tells store.note_chunks to rejoin these pieces
        # with "" rather than the legacy "\n\n" — they already carry their own
        # separators, so a blank-line join would corrupt the note it serves.
        base_meta = {**it["metadata"], "note_id": base, "split": "lossless"}
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
    items, skipped = scan(store)
    chars = sum(len(i["text"]) for i in items)
    print(f"[rechunk] {len(items)} note(s), {chars:,} chars need re-chunking")
    if skipped:
        print(f"[rechunk] {skipped} note(s) skipped — could not verify a lossless "
              f"re-chunk; left untouched as whole-body rows")
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
