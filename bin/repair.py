"""Attended, backup-gated corpus repair (curator-run). Spec 3.

Phases (each independently runnable, each idempotent):

  purge-empty     delete chunks carrying no alphanumeric character
                  (68,193 of 196,396 live chunks; 34.7%)
  reingest-stale  re-fetch and re-chunk Drive files whose chunks predate
                  chunking.CHUNKER_VERSION (455 clipped spreadsheets +
                  9,351 legacy files)
  status          report remaining work; changes nothing

DRY RUN IS THE DEFAULT. Pass --apply to write. --apply takes a full WAL-safe
backup first and refuses if free disk is under twice the database size: the
store is ~11 GB, and a previous session filled this machine's disk to zero
copying it, froze the machine, and the emergency cleanup destroyed an unrelated
application's data.

After --apply, run the gold gate and restore the printed backup if it regresses.
Every phase is resumable — the selectors are level-triggered, so an interrupted
run is simply re-run.
"""

import argparse
import shutil
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from mcpbrain import config                     # noqa: E402
from mcpbrain.backup import snapshot            # noqa: E402
from mcpbrain.chunking import CHUNKER_VERSION   # noqa: E402
from mcpbrain.store import Store                # noqa: E402

_PURGE_BATCH = 5000


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def preflight(db_path: Path, *, db_bytes: int | None = None) -> tuple[bool, str]:
    """Refuse to --apply unless a backup can safely fit.

    Twice the database size: one for the backup, one for headroom (SQLite needs
    room for its WAL and for the snapshot's own temporary files).
    """
    if db_bytes is None:
        db_bytes = db_path.stat().st_size if db_path.exists() else 0
    need = db_bytes * 2
    free = _free_bytes(db_path.parent)
    if free < need:
        return False, (f"insufficient disk: need ~{need // 1024**3} GB "
                       f"(2x the {db_bytes // 1024**3} GB store), "
                       f"{free // 1024**3} GB free")
    return True, ""


def _backup(db_path: Path) -> Path:
    # WAL-safe: the store runs journal_mode=WAL, so a plain file copy can MISS
    # committed transactions. backup.snapshot uses the SQLite backup API.
    dest = db_path.with_suffix(db_path.suffix + f".bak-{int(time.time())}")
    return snapshot(db_path, dest)


def phase_status(store, apply: bool) -> int:
    empty = store.count_content_free()
    total = store.chunk_count()
    stale = len(store.stale_chunker_file_ids(CHUNKER_VERSION, limit=100_000))
    print(f"content-free chunks       : {empty} of {total} "
          f"({100 * empty / total:.1f}%)" if total else f"content-free: {empty}")
    print(f"Drive files to re-chunk   : {stale} (chunker_version < {CHUNKER_VERSION})")
    return 0


def phase_purge_empty(store, apply: bool) -> int:
    total = store.count_content_free()
    print(f"[purge-empty] {total} content-free chunk(s) match")
    if not apply:
        print("[purge-empty] dry run — nothing deleted; pass --apply to write")
        return 0
    done = 0
    while True:
        batch = store.content_free_doc_ids(limit=_PURGE_BATCH)
        if not batch:
            break
        # purge_doc_ids raises, deleting NOTHING in this batch, if the graph
        # cites any id in it (all-or-nothing, by design — see tests/test_purge.py).
        # This measured zero cited doc_ids among the 68,193 content-free chunks
        # on the live store on 2026-07-28, but the check runs at apply time
        # rather than trusting that measurement — a hard halt here is correct
        # (the alternative is silently orphaning graph provenance), it just must
        # not surface as a bare traceback: print which id(s) are cited and stop.
        try:
            done += store.purge_doc_ids(batch)
        except ValueError as exc:
            print(f"[purge-empty] refusing this batch: {exc}", file=sys.stderr)
            print(f"[purge-empty] deleted {done} before halting; investigate the "
                  "cited doc_id(s) above before re-running", file=sys.stderr)
            return 4
        print(f"[purge-empty] {done}/{total}")
    print(f"[purge-empty] deleted {done}")
    return 0


def phase_reingest_stale(store, apply: bool, *, limit: int) -> int:
    ids = store.stale_chunker_file_ids(CHUNKER_VERSION, limit=limit)
    print(f"[reingest-stale] {len(ids)} Drive file(s) selected (limit {limit})")
    if not apply:
        print("[reingest-stale] dry run — nothing fetched; pass --apply to write")
        return 0
    from mcpbrain.auth import build_google_services
    from mcpbrain.sync.drive import reingest_files
    services = build_google_services()
    drive = services.get("drive_service")
    if drive is None:
        # build_google_services omits a service whose scope the token lacks,
        # rather than failing the whole build — so this is a missing scope, not
        # a crash, and it must be said plainly.
        print("[reingest-stale] no drive_service (token lacks the Drive scope); "
              "re-authenticate with `mcpbrain setup`", file=sys.stderr)
        return 0
    print(f"[reingest-stale] {reingest_files(drive, store, ids)}")
    return 0


_PHASES = {"status": phase_status, "purge-empty": phase_purge_empty,
           "reingest-stale": phase_reingest_stale}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("phase", choices=sorted(_PHASES))
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    ap.add_argument("--limit", type=int, default=500,
                    help="max Drive files per reingest-stale run")
    args = ap.parse_args(argv)

    home = config.app_dir()
    db_path = Path(home) / "brain.sqlite3"
    if not db_path.exists():
        print(f"no store at {db_path}", file=sys.stderr)
        return 2

    if args.apply:
        ok, why = preflight(db_path)
        if not ok:
            print(f"[repair] refusing to apply: {why}", file=sys.stderr)
            return 3
        backup = _backup(db_path)
        print(f"[repair] backup written: {backup}")

    # Dim comes from the embedder, exactly as bin/consolidate.py:51 does it —
    # there is no config.embed_dim; the org pin's `dim` is a fleet-baseline
    # field, not this install's live dimension.
    from mcpbrain.embed import get_embedder
    store = Store(db_path, dim=get_embedder("bge-small").dim)
    fn = _PHASES[args.phase]
    if args.phase == "reingest-stale":
        rc = fn(store, args.apply, limit=args.limit)
    else:
        rc = fn(store, args.apply)

    if rc:
        # A phase halted deliberately (e.g. purge-empty hit a graph-cited
        # batch) and already printed why. Surface that as this process's exit
        # code rather than also printing the gold-gate reminder below, which
        # only makes sense after a clean run.
        return rc

    if args.apply:
        print("\n[repair] Run the gold gate now (PRODUCTION path):\n"
              "  uv run python tests/eval/run_eval.py --gold --k 10\n"
              "  Baseline 2026-07-28: recall@10 0.700 / MRR 0.510.\n"
              f"  If it regresses, restore:  cp {backup} {db_path}\n"
              "  Once it passes and you are satisfied, delete the backup — it is\n"
              "  a full copy of the store and this machine has limited headroom.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
