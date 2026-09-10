#!/usr/bin/env python3
"""Stamp audited v0 prose chunks as current, preserving what they really were.

ATTENDED ONLY. Dry-run by default; --yes required. Stop the daemon first.
Takes its own WAL-safe snapshot before writing.

WHY. `stale_chunker_ids` selects every chunk below the chunker floor so
`bin/repair.py reingest-stale` can re-fetch it. On this store that was 44,581
items, and 41,530 of them were v0 PROSE with no detectable defect — re-fetching
those would have burned API quota and discarded enrichment on ~60,500 chunks to
re-derive text that is not broken.

The v1->v2 change was specific: "chunk_text no longer emits empty or oversize
chunks; tabular sources are chunked by header-repeating row group instead of
character-split; content-free text is never written as a chunk." Audited against
the live store (2026-09-10), ALL THREE are inapplicable to this set: 0 chunks are
content-free, the oversize ones are handled by the reingest sweep, and prose is
not tabular. So these chunks are OLD, not DAMAGED.

WHAT THIS IS NOT. Stamping `chunker_version` alone would assert these were
produced by the current chunker. They were not, and that claim is unrecoverable:
if a future bump ever fixes something in prose, the sweep would skip them forever
with no way to find them again. So every stamped chunk also gets

    chunker_upgraded_from : the version it ACTUALLY carried (0)
    chunker_audited       : ISO date of the audit that accepted it

which makes the set findable later:

    SELECT ... WHERE json_extract(metadata,'$.chunker_upgraded_from') IS NOT NULL

ORDERING. Run this BEFORE `bin/repair.py reingest-stale`. Stamping drops these
chunks out of the stale selector, so the sweep then targets only the genuinely
damaged set with no new filter flag. A file whose table chunks ARE re-fetched has
all its chunks replaced anyway, which re-derives any prose stamped here — that is
harmless, and the provenance simply disappears with the replaced row.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcpbrain import config                                              # noqa: E402
from mcpbrain.backup import snapshot                                     # noqa: E402
from mcpbrain.chunking import CHUNKER_VERSION, PRIOR_CHUNKER_VERSION     # noqa: E402
from mcpbrain.store import Store, store_dim_from_path                    # noqa: E402

OVERSIZE_CHARS = 2000   # doctor's embedder-window threshold; these go to reingest

_V = "COALESCE(json_extract(metadata,'$.chunker_version'),0)"
_TABLE = "COALESCE(json_extract(metadata,'$.content_subtype'),'')='table'"

# Audited set: below the prose floor, not tabular, within the embedder window,
# and carrying actual content. Anything failing one of those stays stale so the
# reingest sweep still sees it.
SELECT_AUDITED = (
    f"{_V} < ? AND NOT {_TABLE} AND length(text) <= ? "
    f"AND NOT is_content_free(text)"
)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--yes", action="store_true",
                    help="actually write; without it this is a dry run")
    ap.add_argument("--home", default=None, help="override MCPBRAIN_HOME")
    args = ap.parse_args(argv)

    path = (Path(args.home) / "brain.sqlite3") if args.home else config.store_path()
    store = Store(path, dim=store_dim_from_path(path), read_only=not args.yes)

    with store._connect() as db:
        db.create_function("is_content_free", 1, store._content_free)
        params = (PRIOR_CHUNKER_VERSION, OVERSIZE_CHARS)
        n = db.execute(f"SELECT COUNT(*) FROM chunks WHERE {SELECT_AUDITED}",
                       params).fetchone()[0]
        left_stale = db.execute(
            f"SELECT COUNT(*) FROM chunks WHERE "
            f"(({_V} < ?) OR ({_V} < ? AND {_TABLE})) AND NOT ({SELECT_AUDITED})",
            (PRIOR_CHUNKER_VERSION, CHUNKER_VERSION, *params)).fetchone()[0]

    print(f"audited v0 prose to stamp -> v{CHUNKER_VERSION} : {n:,}")
    print(f"left stale for reingest-stale              : {left_stale:,}")
    if not args.yes:
        print("\ndry run — nothing written; pass --yes to apply")
        return 0
    if not n:
        print("nothing to do")
        return 0

    dest = path.with_suffix(path.suffix + f".bak-{int(time.time())}")
    print(f"\nsnapshotting to {dest.name} ...", flush=True)
    snapshot(path, dest, home=str(path.parent))
    print("  snapshot done")

    stamped = date.today().isoformat()
    with store._connect(write=True) as db:
        db.create_function("is_content_free", 1, store._content_free)
        rows = db.execute(f"SELECT doc_id, metadata FROM chunks WHERE {SELECT_AUDITED}",
                          params).fetchall()
        updates = []
        for r in rows:
            meta = json.loads(r["metadata"])
            # Record the TRUE version before overwriting it. Never clobber an
            # existing provenance stamp — a second run must stay idempotent.
            meta.setdefault("chunker_upgraded_from",
                            int(meta.get("chunker_version") or 0))
            meta.setdefault("chunker_audited", stamped)
            meta["chunker_version"] = CHUNKER_VERSION
            updates.append((json.dumps(meta), r["doc_id"]))
        # chunker_version is NOT a contextual_prefix input (store.patch_chunk_metadata
        # says so explicitly), so fts_context_version stays untouched and these rows
        # are not needlessly re-queued for FTS re-indexing.
        db.executemany("UPDATE chunks SET metadata=? WHERE doc_id=?", updates)
        print(f"stamped {len(updates):,} chunks")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
