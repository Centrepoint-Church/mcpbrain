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
    from mcpbrain.store import Store

    home = args.home or str(config.app_dir())
    store = Store(Path(home) / "brain.sqlite3", dim=4)

    with store._connect() as db:
        rows = db.execute(
            "SELECT rowid, doc_id FROM chunks "
            "WHERE json_extract(metadata,'$.content_subtype')='table' "
            "  AND length(text) > 2000"
        ).fetchall()

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
