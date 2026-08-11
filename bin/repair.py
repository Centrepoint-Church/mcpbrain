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
from mcpbrain.backup import _live_bytes, snapshot  # noqa: E402
from mcpbrain.chunking import CHUNKER_VERSION   # noqa: E402
from mcpbrain.store import Store                # noqa: E402

# Shared --limit default. Meaningful for reingest-stale/backfill-attachments
# (bounds real per-item API cost); overridden to unbounded for
# digest-provenance/embed-pending in main() (see there) since those are pure
# local work with no cost reason to cap.
_DEFAULT_LIMIT = 500

_PURGE_BATCH = 5000


def _free_bytes(path: Path) -> int:
    return shutil.disk_usage(path).free


def preflight(db_path: Path, *, db_bytes: int | None = None) -> tuple[bool, str]:
    """Refuse to --apply unless a backup can safely fit.

    Twice the database size: one for the backup, one for headroom (SQLite needs
    room for its WAL and for the snapshot's own temporary files). Sized from
    LIVE pages (backup._live_bytes), not the file's raw stat().st_size: a
    cache-table dedup can free pages onto SQLite's freelist without shrinking
    the file on disk, and _backup()'s own VACUUM INTO snapshot only ever costs
    the live-page total -- sizing from the raw file would keep refusing
    backups that now actually fit.
    """
    if db_bytes is None:
        db_bytes = _live_bytes(db_path) if db_path.exists() else 0
    need = db_bytes * 2
    free = _free_bytes(db_path.parent)
    if free < need:
        return False, (f"insufficient disk: need ~{need // 1024**3} GB "
                       f"(2x the {db_bytes // 1024**3} GB store), "
                       f"{free // 1024**3} GB free")
    return True, ""


def _backup(db_path: Path) -> Path:
    # WAL-safe: the store runs journal_mode=WAL, so a plain file copy can MISS
    # committed transactions. backup.snapshot() uses VACUUM INTO, which is
    # consistent by construction and cannot be blocked by a held reader.
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
            deleted = store.purge_doc_ids(batch)
        except ValueError as exc:
            print(f"[purge-empty] refusing this batch: {exc}", file=sys.stderr)
            print(f"[purge-empty] deleted {done} before halting; investigate the "
                  "cited doc_id(s) above before re-running", file=sys.stderr)
            return 4
        if not deleted:
            # The selector returned rows but the delete removed none, so the next
            # iteration would select the same batch again: an infinite loop that
            # writes nothing. Realistically a concurrent writer (the daemon)
            # deleted them between the select and the delete, which is benign —
            # but silently spinning forever is not, so stop and say so.
            print(f"[purge-empty] {len(batch)} chunk(s) selected but 0 deleted — "
                  "another writer removed them first; stopping (re-run to "
                  "continue)", file=sys.stderr)
            break
        done += deleted
        print(f"[purge-empty] {done}/{total}")
    print(f"[purge-empty] deleted {done}")
    return 0


def phase_reingest_stale(store, apply: bool, *, limit: int, workers: int = 1) -> int:
    ids = store.stale_chunker_file_ids(CHUNKER_VERSION, limit=limit)
    print(f"[reingest-stale] {len(ids)} Drive file(s) selected (limit {limit})")
    if not apply:
        print("[reingest-stale] dry run — nothing fetched; pass --apply to write")
        return 0
    from mcpbrain.auth import build_google_services
    from mcpbrain.sync.drive import flush_skip_report, reingest_files
    services = build_google_services()
    drive = services.get("drive_service")
    if drive is None:
        # build_google_services omits a service whose scope the token lacks,
        # rather than failing the whole build — so this is a missing scope, not
        # a crash, and it must be said plainly.
        print("[reingest-stale] no drive_service (token lacks the Drive scope); "
              "re-authenticate with `mcpbrain setup`", file=sys.stderr)
        return 0
    # workers>1 parallelizes the network-bound fetch across a thread pool (see
    # reingest_files' docstring); each worker builds its OWN service via this
    # factory rather than sharing `drive`, because googleapiclient's Resource
    # wraps a stateful httplib2.Http that is not safe to use from multiple
    # threads at once.
    service_factory = (
        (lambda: build_google_services().get("drive_service"))
        if workers > 1 else None)
    # Tally skipped files instead of writing one change_log row each, then flush
    # once — the same `report=` pattern every other bulk Drive path uses
    # (sync_drive, backfill_drive, sync_shared_drive all do this). Without it
    # fetch_content's _note_skip takes its immediate-write branch, which would
    # (a) issue store writes from WORKER THREADS, the one hole in
    # reingest_files' otherwise careful "only _apply writes, on the main thread"
    # design, and (b) evict the 500-row change_log — which doubles as the
    # user-facing change digest — with one `ingest_skip` row per unreadable file
    # across a 9,400-file run. Counts may undercount slightly under
    # --workers > 1 (the tally is a plain dict incremented from several threads);
    # they are diagnostics, and the aggregate row is what matters.
    report: dict = {}
    summary = reingest_files(drive, store, ids, max_workers=workers,
                             service_factory=service_factory, report=report)
    flush_skip_report(store, report, source="repair:reingest")
    print(f"[reingest-stale] {summary}")
    return 0


# Cursor key for the attachment backfill's progress. Stored in the generic
# sync_cursors table (same mechanism as gmail's "<source>:resume_ids"), so this
# needs no schema change and survives an interrupted run.
_ATT_CURSOR = "repair:attachments_year"

# Oldest year worth walking. Gmail's `after:`/`before:` need real dates, and a
# per-YEAR window makes the pass resumable at a natural granularity without
# inventing a second cursor format.
_ATT_FLOOR_YEAR = 2008


def phase_backfill_attachments(store, apply: bool, *, limit: int | None,
                               floor_year: int = _ATT_FLOOR_YEAR,
                               all_years: bool = False,
                               workers: int = 1) -> int:
    """Ingest attachments from mail already in the mailbox, newest year first.

    A1 — "a PDF emailed to the user is invisible to the brain, while the
    byte-identical file in Drive is extracted normally", the largest gap in the
    2026-07-27 audit — was fixed in the ingest path, but Gmail sync is
    DELTA-driven: it only ever sees new mail, so historical attachments stayed
    invisible. Measured on the live store after the spec-2/3 work landed: 0
    `email_attachment` chunks. This is the pass that makes the fix real.

    Narrowed server-side with `has:attachment`, so it fetches only
    attachment-bearing mail rather than re-walking the whole mailbox.

    `all_years` walks every year from now back to `floor_year` in ONE run;
    otherwise one year per invocation. Either way the last COMPLETED year is
    recorded in a cursor, so the year loop stays the resume granularity: an
    interrupted --all-years run continues from the year it died in rather than
    restarting the whole history. `limit=None` means "the whole year".

    `workers` > 1 fetches each page of messages concurrently (see
    backfill_gmail): the pass is entirely network-bound — one `messages.get` per
    message plus one `attachments.get` per attachment — so this is the difference
    between minutes and hours over a full mailbox.

    Idempotent throughout: attachment chunks are content-hash keyed, so a message
    processed twice writes the same rows.
    """
    from datetime import datetime, timezone

    done_through = store.get_cursor(_ATT_CURSOR)
    this_year = datetime.now(timezone.utc).year
    start_year = int(done_through) - 1 if done_through else this_year
    if start_year < floor_year:
        print(f"[backfill-attachments] complete: walked back to {floor_year}")
        return 0

    span = (f"{start_year}..{floor_year}" if all_years else str(start_year))
    print(f"[backfill-attachments] years {span} (has:attachment, "
          f"limit {limit if limit is not None else 'none'}, workers {workers})")
    if not apply:
        print("[backfill-attachments] dry run — nothing fetched; "
              "pass --apply to write")
        return 0

    from mcpbrain.auth import build_google_services
    from mcpbrain.sync.gmail import backfill_gmail
    gmail = build_google_services().get("gmail_service")
    if gmail is None:
        print("[backfill-attachments] no gmail_service (token lacks the Gmail "
              "scope); re-authenticate with `mcpbrain setup`", file=sys.stderr)
        return 0
    # Each worker needs its OWN Resource: googleapiclient wraps a stateful
    # httplib2.Http that is not safe to share across threads. Same reason
    # bin/repair.py's reingest-stale passes a factory.
    factory = ((lambda: build_google_services().get("gmail_service"))
               if workers > 1 else None)

    total = 0
    years = (range(start_year, floor_year - 1, -1) if all_years else [start_year])
    for year in years:
        n = backfill_gmail(gmail, store, after=f"{year}/01/01",
                           before=f"{year + 1}/01/01", max_messages=limit,
                           q_extra="has:attachment", max_workers=workers,
                           service_factory=factory)
        total += n
        # Advance only after the window's writes are durable, mirroring
        # sync_gmail's cursor discipline. A year that hit its limit is NOT
        # finished, so the cursor stays put and the next run re-walks it —
        # already-written messages are content-hash idempotent, so that costs
        # fetches, not correctness.
        if limit is not None and n >= limit:
            print(f"[backfill-attachments] year {year} hit the {limit}-message "
                  f"limit ({n} done); re-run to continue the same year")
            break
        store.set_cursor(_ATT_CURSOR, str(year))
        print(f"[backfill-attachments] year {year}: {n} message(s) "
              f"(running total {total})")
    else:
        if all_years:
            print(f"[backfill-attachments] complete: walked back to {floor_year}; "
                  f"{total} message(s) total")
    return 0


def phase_embed_pending(store, apply: bool, *, limit: int | None,
                        batch_size: int = 32) -> int:
    """Embed every pending chunk in one pass, at full speed.

    Why this exists. Measured on this machine: the embedder does ~66 chunks/sec
    in isolation (batch size is irrelevant — 32/64/128/256 all land within noise
    of each other), while the daemon achieves ~5. The daemon is not doing
    anything wrong; index_pending simply gets a slice of a 60s cycle that is
    mostly sync, drain, prepare, cadences and periodic backup, and it is
    deliberately bounded (EMBED_MAX_ITEMS, CYCLE_BUDGET_S) so recall stays
    responsive. That is right for steady state and wrong for a backlog: 15,307
    attachment chunks arriving at once is ~50 minutes of CPU spread over hours of
    daemon uptime, during which the new content is not searchable.

    So this drains in one attended pass — no cycle budget, no item cap, looping
    until nothing is pending.

    The daemon is PAUSED for the duration. SQLite here is single-writer, and
    running a second embedding writer alongside the daemon's own is exactly the
    contention the architecture exists to avoid; pausing is cheaper and more
    honest than racing it and relying on busy_timeout. It always resumes, even if
    embedding raises — a repair that leaves the brain paused has broken the thing
    it was fixing. A daemon that is not running at all is the easy case, not an
    error.
    """
    from mcpbrain import config
    from mcpbrain.index import index_pending

    pending = store.count_pending_embeddings() if hasattr(
        store, "count_pending_embeddings") else None
    print(f"[embed-pending] {pending if pending is not None else 'unknown'} "
          f"chunk(s) awaiting embedding (batch {batch_size})")
    if not apply:
        print("[embed-pending] dry run — nothing embedded; pass --apply to write")
        return 0

    from mcpbrain.control_client import ControlClient, DaemonUnavailable
    client = None
    try:
        client = ControlClient(str(config.app_dir()), timeout=30)
        client.pause()
        print("[embed-pending] daemon paused (single-writer store)")
    except DaemonUnavailable:
        client = None
        print("[embed-pending] daemon not running — no contention to avoid")
    except Exception as exc:  # noqa: BLE001 — pausing is an optimisation
        client = None
        print(f"[embed-pending] could not pause the daemon ({exc}); continuing — "
              f"expect slower progress while both processes write", file=sys.stderr)

    total = 0
    try:
        from mcpbrain.embed import get_embedder
        embedder = get_embedder()
        while True:
            n = index_pending(store, embedder, batch_size,
                              home=str(config.app_dir()),
                              budget=None, max_items=None)
            if not n:
                break
            total += n
            print(f"[embed-pending] {total} embedded")
            if limit is not None and total >= limit:
                print(f"[embed-pending] stopping at the {limit}-chunk limit")
                break
    finally:
        if client is not None:
            try:
                client.resume()
                print("[embed-pending] daemon resumed")
            except Exception as exc:  # noqa: BLE001
                print(f"[embed-pending] COULD NOT RESUME THE DAEMON ({exc}) — "
                      f"run `mcpbrain doctor --repair` or restart it",
                      file=sys.stderr)
    print(f"[embed-pending] done: {total} chunk(s) embedded")
    return 0


def phase_digest_provenance(store, apply: bool, *, limit: int | None) -> int:
    """Repair enriched-digest date/source_type deterministically — no model calls.

    C2 and C4 were fixed in build_semantic_doc, which only runs on RE-enrichment,
    so neither reached the 22,357 digests already stored: 0 of them carried a
    date, so importance.recency_decay returned its neutral 0.5 for the
    LLM-digested summaries — the highest-value chunks in the store.

    Forcing re-enrichment to fix that was the wrong instrument. `date` is
    recoverable from the digest's own "Date:" line for ~70% of them, and
    `source_type` is 100% derivable from `thread_id`; meanwhile 19,934 of 21,029
    email digests have had their source chunks pruned by retention, so
    re-enrichment could not recover a date for the remainder either. This costs
    nothing and fixes the same population.

    Uses patch_chunk_metadata, so `content_hash` and `embedded` are untouched:
    nothing re-embeds and nothing re-queues. Idempotent — derive_patch returns {}
    for an already-correct digest, so a second run is a no-op.
    """
    from mcpbrain.digest_provenance import derive_patch

    digests = store.digest_chunks(limit=limit)
    print(f"[digest-provenance] {len(digests)} digest chunk(s) to examine")
    patches = [(d["doc_id"], derive_patch(d)) for d in digests]
    todo = [(doc_id, p) for doc_id, p in patches if p]
    dates = sum(1 for _d, p in todo if "date" in p)
    labels = sum(1 for _d, p in todo if "source_type" in p)
    print(f"[digest-provenance] {len(todo)} need a patch "
          f"({dates} gain a date, {labels} a corrected source_type)")
    if not apply:
        print("[digest-provenance] dry run — nothing written; pass --apply to write")
        return 0
    done = 0
    for doc_id, patch in todo:
        if store.patch_chunk_metadata(doc_id, **patch):
            done += 1
    print(f"[digest-provenance] patched {done} digest(s)")
    return 0


_PHASES = {"status": phase_status, "purge-empty": phase_purge_empty,
           "reingest-stale": phase_reingest_stale,
           "backfill-attachments": phase_backfill_attachments,
           "embed-pending": phase_embed_pending,
           "digest-provenance": phase_digest_provenance}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("phase", choices=sorted(_PHASES))
    ap.add_argument("--apply", action="store_true",
                    help="actually write (default is a dry run)")
    ap.add_argument("--limit", type=int, default=_DEFAULT_LIMIT,
                    help="max items per run (Drive files for reingest-stale, "
                         "messages per year for backfill-attachments). "
                         "0 means no limit. digest-provenance and embed-pending "
                         "are unbounded by default regardless (pure local work, "
                         "no API cost) — pass --limit explicitly to cap them.")
    ap.add_argument("--workers", type=int, default=1,
                    help="concurrent fetch workers for reingest-stale and "
                         "backfill-attachments (default 1 = sequential). Both are "
                         "network-bound; writes always stay on one thread.")
    ap.add_argument("--all-years", action="store_true",
                    help="backfill-attachments: walk every year back to the floor "
                         "in ONE run instead of one year per invocation. The "
                         "per-year cursor still advances, so an interrupted run "
                         "resumes from the year it stopped in.")
    ap.add_argument("--skip-backup", action="store_true",
                    help="skip the pre-apply backup/disk-preflight for THIS "
                         "invocation. For running many reingest-stale batches "
                         "back to back under one backup taken up front, not for "
                         "routine use: each batch is still write-through to the "
                         "live store, so anything before the one backup you did "
                         "take is what --skip-backup runs are gambling on.")
    args = ap.parse_args(argv)
    # `--limit 0` means "no limit" for any phase — 0 as a real bound would do
    # nothing, so it is the natural sentinel.
    if args.limit == 0:
        args.limit = None
    elif args.limit == _DEFAULT_LIMIT and args.phase in (
            "digest-provenance", "embed-pending"):
        # These two are pure local work: no network call, no model call, so
        # there is no cost reason to cap them. The shared 500 default exists for
        # reingest-stale/backfill-attachments, which spend real API quota per
        # item — applying it here silently would leave a user who ran
        # `digest-provenance --apply` believing they had fixed all 22,357
        # digests when only the first 500 (by rowid) were touched.
        args.limit = None

    home = config.app_dir()
    db_path = Path(home) / "brain.sqlite3"
    if not db_path.exists():
        print(f"no store at {db_path}", file=sys.stderr)
        return 2

    # `status` never writes, so --apply on it is a no-op — and a backup is a full
    # copy of an ~11 GB store: minutes of I/O plus 2x the disk (which this
    # machine does not have to spare) for a read-only report. Preflight is
    # skipped with it, since its only purpose is proving the backup fits.
    # --skip-backup skips the same way, on request, for a caller who already
    # took one backup covering a whole sequence of --apply runs.
    backup = None
    if args.apply and args.phase != "status" and not args.skip_backup:
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
        rc = fn(store, args.apply, limit=args.limit, workers=args.workers)
    elif args.phase == "backfill-attachments":
        rc = fn(store, args.apply, limit=args.limit, all_years=args.all_years,
                workers=args.workers)
    elif args.phase in ("embed-pending", "digest-provenance"):
        rc = fn(store, args.apply, limit=args.limit)
    else:
        rc = fn(store, args.apply)

    if rc:
        # A phase halted deliberately (e.g. purge-empty hit a graph-cited
        # batch) and already printed why. Surface that as this process's exit
        # code rather than also printing the gold-gate reminder below, which
        # only makes sense after a clean run.
        return rc

    if backup is not None:
        print("\n[repair] Run the gold gate now (PRODUCTION path):\n"
              "  uv run python tests/eval/run_eval.py --gold --k 10\n"
              "  Baseline 2026-07-28: recall@10 0.700 / MRR 0.510.\n"
              f"  If it regresses, restore:  cp {backup} {db_path}\n"
              "  Once it passes and you are satisfied, delete the backup — it is\n"
              "  a full copy of the store and this machine has limited headroom.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
