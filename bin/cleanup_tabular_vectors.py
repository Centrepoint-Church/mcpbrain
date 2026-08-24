#!/usr/bin/env python3
"""One-shot cleanup: delete vec_chunks rows for oversize table-subtype chunks.

The tabular renderer bug (phantom-column-inflated width, fixed in
mcpbrain/sync/tabular.py) produced chunks up to 293KB, each holding a
near-duplicate low-quality vector (title text + garbage) once truncated by
the embedder. The CHUNKER_VERSION bump means every one of these gets
re-fetched and re-rendered by `bin/repair.py reingest-stale` eventually, but
that runs at the daemon's normal backfill pace -- this script deletes the
existing garbage vectors immediately so they stop polluting dense search
right away, ahead of the full re-fetch.

Only the vec_chunks row goes; `embedded=1` is left alone deliberately, so
index_pending does not immediately re-embed the same garbage text before the
re-render lands. That leaves an `embedded=1` chunk with no vector, which
`backup._verify_artifact` treats as designed rather than corrupt precisely
because these are content_subtype='table' rows -- see its docstring before
widening this script's WHERE clause to any other subtype.

Dry-run by default; pass --apply to actually delete. Matches the
bin/relocate_ingest_cache.py / bin/consolidate.py convention.
"""
import argparse
import sys


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--home", default=None,
                    help="MCPBRAIN_HOME override (defaults to config.app_dir())")
    ap.add_argument("--apply", action="store_true",
                    help="actually delete; default is dry-run (report only)")
    args = ap.parse_args(argv)

    from pathlib import Path
    from mcpbrain import config
    from mcpbrain.chunking import CHUNKER_VERSION
    from mcpbrain.store import Store

    home = args.home or str(config.app_dir())
    store = Store(Path(home) / "brain.sqlite3", dim=4)

    with store._connect() as db:
        rows = db.execute(
            "SELECT rowid, doc_id FROM chunks "
            "WHERE json_extract(metadata,'$.content_subtype')='table' "
            "  AND length(text) > 2000 "
            # A chunk already at CHUNKER_VERSION has already been through the
            # new renderer -- if it's still over 2000 chars, that's a
            # legitimately dense row group, not garbage from the old
            # phantom-column bug. Deleting its vector would be permanent:
            # `embedded` stays 1, so the version-gated stale_chunker_ids
            # selector never re-queues it for embedding again.
            "  AND COALESCE(json_extract(metadata,'$.chunker_version'),0) < ?"
        , (CHUNKER_VERSION,)).fetchall()

    print(f"[cleanup-tabular-vectors] {len(rows)} oversize table chunk(s) found")
    if not args.apply:
        print("[cleanup-tabular-vectors] dry run — nothing deleted; "
              "pass --apply to delete their vec_chunks rows")
        return 0

    with store._connect(write=True) as db:
        for r in rows:
            db.execute("DELETE FROM vec_chunks WHERE rowid=?", (r["rowid"],))
    print(f"[cleanup-tabular-vectors] deleted {len(rows)} vec_chunks row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
