#!/usr/bin/env python3
"""Is VACUUM INTO a viable backup mechanism on the real store?

THE QUESTION
------------
backup.snapshot() no longer runs wal_checkpoint(TRUNCATE) and no longer copies
the DB file; it builds the artifact with VACUUM INTO. Two things that can only
be answered against the real 15.65GB store decide whether it ships:

  1. DURATION. A logical rebuild is slower per byte than a file copy. The
     backup runs on the daemon's cycle thread, so a rebuild approaching
     STALL_S (1800s, daemon.py:169) means redesign, not ship.
  2. FIDELITY. VACUUM rebuilds every b-tree, including vec0's shadow tables.
     vec_chunks_vector_chunks00 is declared untyped `rowid PRIMARY KEY` and
     holds 274 rows across rowids 1..450 -- 176 gaps a renumbering VACUUM
     could close, while vec_chunks_chunks.chunk_id is INTEGER PRIMARY KEY and
     would not move. A synthetic probe measured rowids preserved and KNN
     identical; this repeats it at real scale (167,992 vectors, dim 384).

The pinned_reader arm is the direct proof that cause (R) is closed: before this
work, snapshot() inside that window raised
`RuntimeError: wal_checkpoint(TRUNCATE) busy=1`.

SAFETY
------
Read-only against the live store apart from the artifact it writes into a temp
dir and deletes. It never moves, replaces or deletes brain.sqlite3, and it
holds no write lock. The pinned reader is a mode=ro connection, exactly as
bin/probe_wal_contention.py's arm of the same name -- a read-only connection
cannot write, cannot checkpoint, and in WAL mode does not block writers, so it
is safe to hold against the real store while the daemon is up.
"""
import argparse
import json
import resource
import shutil
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from mcpbrain import backup                      # noqa: E402
from mcpbrain.config import app_dir              # noqa: E402
from mcpbrain.store import _open_db              # noqa: E402

STORE = Path(app_dir()) / "brain.sqlite3"
QUERY_SEEDS = (11, 2731, 90210)


def _sizes():
    wal = Path(f"{STORE}-wal")
    return {"file_mb": STORE.stat().st_size // 2**20,
            "live_mb": backup._live_bytes(STORE) // 2**20,
            "wal_mb": (wal.stat().st_size // 2**20) if wal.exists() else 0}


def _peak_rss_mb():
    # macOS reports ru_maxrss in bytes, Linux in KiB.
    raw = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return raw // 2**20 if sys.platform == "darwin" else raw // 1024


def _dim(db):
    row = db.execute("SELECT embedding FROM vec_chunks LIMIT 1").fetchone()
    return len(bytes(row[0])) // 4 if row else 0


def _query(seed, dim):
    return struct.pack(f"{dim}f", *[((seed * 7919 + j * 104729) % 1000) / 1000.0
                                    for j in range(dim)])


def _fingerprint(path):
    """Everything a broken rebuild would change, read from one database."""
    db = _open_db(path, read_only=False)
    try:
        dim = _dim(db)
        out = {"chunks": db.execute("SELECT count(*) FROM chunks").fetchone()[0],
               "vec_rowids": db.execute(
                   "SELECT count(*) FROM vec_chunks_rowids").fetchone()[0],
               "dim": dim,
               "fts": db.execute("SELECT count(*) FROM fts_chunks "
                                 "WHERE fts_chunks MATCH 'the'").fetchone()[0],
               "knn": {}}
        for seed in QUERY_SEEDS:
            rows = db.execute(
                "SELECT c.doc_id, v.distance FROM vec_chunks v "
                "JOIN chunks c ON c.rowid = v.rowid "
                "WHERE v.embedding MATCH ? AND k = 10 ORDER BY v.distance",
                (_query(seed, dim),)).fetchall()
            out["knn"][seed] = [(r["doc_id"], round(r["distance"], 5)) for r in rows]
        return out
    finally:
        db.close()


def arm_baseline(work):
    """The mechanism being replaced, sized WITHOUT running it.

    shutil.copy2 of the store needs file_mb of free space. On this box that is
    15.65GB against 13.19GB free, so it cannot run at all -- which is not a
    limitation of this probe, it is the finding: the old mechanism is
    unrunnable here, and that is what the ENOSPC preflight has been reporting.
    The freelist does NOT change this; freeing pages leaves the file the same
    size on disk.

    So this measures read throughput over the store and reports what a copy
    would have required, rather than pretending to a number it cannot obtain.
    """
    free_mb = shutil.disk_usage(str(work)).free // 2**20
    file_mb = STORE.stat().st_size // 2**20
    t0 = time.monotonic()
    read = 0
    with STORE.open("rb") as fh:
        while chunk := fh.read(8 * 1024 * 1024):
            read += len(chunk)
    elapsed = time.monotonic() - t0
    return {"arm": "baseline_copy2",
            "read_seconds": round(elapsed, 1),
            "read_mb_per_s": round((read / 2**20) / max(elapsed, 0.001), 1),
            "copy2_would_need_mb": file_mb, "free_mb": free_mb,
            "runnable": file_mb < free_mb,
            "note": ("copy2 cannot run here: the store file exceeds free disk. "
                     "A copy writes as well as reads, so its wall time is at "
                     "least the read time above.")}


def arm_snapshot(work, keep=False):
    dest = work / "snapshot.sqlite3"
    before = _sizes()
    t0 = time.monotonic()
    backup.snapshot(STORE, dest)
    elapsed = time.monotonic() - t0
    res = {"arm": "snapshot_vacuum_into", "seconds": round(elapsed, 1),
           "artifact_mb": dest.stat().st_size // 2**20,
           "peak_rss_mb": _peak_rss_mb(),
           "wal_mb_before": before["wal_mb"], "wal_mb_after": _sizes()["wal_mb"],
           "stall_s_budget": 1800.0}
    res["gate"] = "PASS" if elapsed < 900 else "REVIEW — over half of STALL_S"
    if not keep:
        dest.unlink(missing_ok=True)
    return res


def arm_pinned_reader(work):
    """Cause (R): one held read transaction. This raised busy=1 before."""
    dest = work / "pinned.sqlite3"
    reader = _open_db(STORE, read_only=True)
    reader.execute("BEGIN")
    reader.execute("SELECT count(*) FROM chunks").fetchone()
    try:
        wal = Path(f"{STORE}-wal")
        wal_mb = (wal.stat().st_size // 2**20) if wal.exists() else 0
        t0 = time.monotonic()
        try:
            backup.snapshot(STORE, dest)
            outcome = "SUCCESS — cause (R) is closed"
        except Exception as exc:                      # noqa: BLE001
            outcome = f"FAILED — {type(exc).__name__}: {exc}"
        return {"arm": "pinned_reader", "seconds": round(time.monotonic() - t0, 1),
                "wal_mb_at_start": wal_mb, "outcome": outcome}
    finally:
        reader.rollback()
        reader.close()
        dest.unlink(missing_ok=True)


def arm_fidelity(work):
    dest = work / "fidelity.sqlite3"
    src_before = _fingerprint(STORE)
    backup.snapshot(STORE, dest)
    art = _fingerprint(dest)
    src_after = _fingerprint(STORE)
    dest.unlink(missing_ok=True)
    # The daemon may write during the rebuild, so counts can legitimately move.
    # KNN over the top of a 167k-vector index should not.
    knn_match = all(art["knn"][s] == src_before["knn"][s] for s in QUERY_SEEDS)
    return {"arm": "fidelity", "source_before": src_before["chunks"],
            "source_after": src_after["chunks"], "artifact": art["chunks"],
            "dim": art["dim"], "fts_source": src_before["fts"],
            "fts_artifact": art["fts"],
            "knn_identical": knn_match,
            "gate": "PASS" if knn_match else "FAIL — artifact KNN differs"}


ARMS = {"baseline": arm_baseline, "snapshot": arm_snapshot,
        "pinned_reader": arm_pinned_reader, "fidelity": arm_fidelity}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", nargs="*", default=["baseline", "snapshot",
                                                  "pinned_reader", "fidelity"])
    ap.add_argument("--all", action="store_true")
    args = ap.parse_args()

    work = Path(tempfile.mkdtemp(prefix="mcpbrain-probe-"))
    print(json.dumps({"store": _sizes()}, indent=2))
    try:
        for name in (ARMS if args.all else args.arms):
            print(json.dumps(ARMS[name](work), indent=2), flush=True)
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    main()
