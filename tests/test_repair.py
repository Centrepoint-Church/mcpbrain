"""The repair CLI is attended and destructive; its guardrails are the tests.

Precedent: bin/consolidate.py — 91 lines, backup first, gold gate printed, all
logic in the library.
"""
import subprocess
import sys

import pytest
from pathlib import Path

_BIN = Path(__file__).resolve().parents[1] / "bin" / "repair.py"


def _run(*args, home):
    return subprocess.run([sys.executable, str(_BIN), *args],
                          capture_output=True, text=True,
                          env={"MCPBRAIN_HOME": str(home), "PATH": ""})


def test_dry_run_is_the_default(tmp_path):
    """Nothing destructive may happen without an explicit --apply. The one
    guardrail that matters most: this operates on an 11 GB irreplaceable store."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "|  |  |", "h1", {})

    out = _run("purge-empty", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "dry run" in out.stdout.lower()
    assert Store(tmp_path / "brain.sqlite3", dim=4).get_chunk("d1") is not None


def test_apply_refuses_without_enough_free_disk(tmp_path, monkeypatch):
    """The backup is a full copy of an 11 GB file. A previous session filled this
    machine's disk to zero copying that database and the emergency cleanup
    destroyed an unrelated application's data. Refuse rather than risk it."""
    import bin.repair as repair

    monkeypatch.setattr(repair, "_free_bytes", lambda path: 1024)

    ok, why = repair.preflight(tmp_path / "brain.sqlite3", db_bytes=11 * 1024**3)

    assert ok is False
    assert "disk" in why.lower()


def test_preflight_sizes_from_live_pages_not_file_size(tmp_path, monkeypatch):
    """Same hazard as backup._require_free_space: a cache-table dedup can free
    pages onto SQLite's freelist without shrinking the file on disk. This
    preflight guards the same snapshot() call and must size from live pages
    (backup._live_bytes), not db_path.stat().st_size, or it keeps refusing a
    backup that now actually fits."""
    import bin.repair as repair
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    for i in range(4000):
        store.upsert_chunk(f"d-{i}", "x" * 2000, f"h{i}", {})
    store.delete_chunks([f"d-{i}" for i in range(3600)])   # big freelist, no VACUUM

    file_bytes = (tmp_path / "brain.sqlite3").stat().st_size
    live_bytes = repair._live_bytes(tmp_path / "brain.sqlite3")
    assert live_bytes < file_bytes * 0.7, (
        f"NULL INSTRUMENT: freelist too small to tell the two apart "
        f"(live={live_bytes} file={file_bytes})")

    # Enough free disk for 2x the LIVE size, but not for 2x the raw FILE size --
    # the old file-size-based estimate would refuse this; the fixed one must not.
    monkeypatch.setattr(repair, "_free_bytes", lambda path: int(live_bytes * 2.5))
    ok, why = repair.preflight(tmp_path / "brain.sqlite3")

    assert ok is True, why


def test_purge_reports_what_it_would_do_without_doing_it(tmp_path):
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    for i in range(3):
        store.upsert_chunk(f"d{i}", "|  |  |", f"h{i}", {})
    store.upsert_chunk("keep", "real content", "hk", {})

    out = _run("purge-empty", home=tmp_path)

    assert "3" in out.stdout
    assert Store(tmp_path / "brain.sqlite3", dim=4).count_content_free() == 3


def test_reingest_phase_reports_the_stale_file_count(tmp_path):
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gdrive-f1-0", "legacy", "h1",
                       {"source_type": "gdrive", "file_id": "f1"})

    out = _run("reingest-stale", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "1" in out.stdout


def test_an_unknown_phase_exits_nonzero(tmp_path):
    out = _run("delete-everything", home=tmp_path)

    assert out.returncode != 0


def test_purge_apply_halts_cleanly_on_a_graph_cited_chunk(tmp_path):
    """purge_doc_ids raises ValueError, deleting NOTHING in that batch, if the
    graph cites any doc_id in it (all-or-nothing, by design — see
    tests/test_purge.py::test_purge_is_all_or_nothing). This measured zero
    cited doc_ids among the 68,193 live content-free chunks on 2026-07-28, but
    if it ever DOES fire the CLI must not let a bare traceback through: it must
    print which doc_id is cited and exit non-zero, and — since purge_doc_ids is
    all-or-nothing — every content-free chunk in that same batch (cited or not)
    must survive untouched rather than be silently orphaned or partially purged."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d-cited", "|  |  |", "h1", {})
    store.upsert_chunk("d-uncited", "|  |  |", "h2", {})
    with store._connect(write=True) as db:
        db.execute("INSERT INTO entity_relations"
                   "(entity_a,relation,entity_b,source_doc_id,valid_from) "
                   "VALUES('e1','mentioned_with','e2','d-cited','2026-01-01')")

    out = _run("purge-empty", "--apply", home=tmp_path)

    assert out.returncode != 0
    assert "d-cited" in (out.stdout + out.stderr)
    # The presentational requirement, not just "it happens to fail": no raw
    # Python stack trace — the ValueError must be caught and reported as a
    # one-line CLI message, not let through as an uncaught-exception crash
    # (whose default traceback would ALSO happen to mention "d-cited", since
    # that's inside the exception's own str() — so this is the assertion that
    # actually distinguishes "handled" from "crashed").
    assert "Traceback (most recent call last)" not in out.stderr
    assert "refusing" in (out.stdout + out.stderr).lower()
    reopened = Store(tmp_path / "brain.sqlite3", dim=4)
    assert reopened.get_chunk("d-cited") is not None, "cited chunk must survive"
    assert reopened.get_chunk("d-uncited") is not None, (
        "purge_doc_ids is all-or-nothing: nothing in the same batch is deleted")


def test_status_with_apply_takes_no_backup(tmp_path):
    """`status` writes nothing, so --apply on it is a no-op — but the backup is a
    full copy of an ~11 GB store: minutes of I/O and 2x the disk (which this
    machine does not have spare) for a read-only report."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "|  |  |", "h1", {})

    out = _run("status", "--apply", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "backup written" not in out.stdout.lower(), out.stdout
    assert list(tmp_path.glob("brain.sqlite3.bak-*")) == []
    # ...and with no backup to restore from, the gold-gate/restore block that
    # names one must not be printed either.
    assert "restore" not in out.stdout.lower()


def test_skip_backup_takes_no_backup_and_skips_preflight(tmp_path, monkeypatch):
    """For running many reingest-stale batches back to back under ONE backup
    taken up front, rather than one full ~11 GB backup per batch (which fills
    the disk after a handful of runs). --skip-backup must also skip preflight
    -- its only purpose is proving a backup that isn't being taken will fit --
    so this must succeed even when free disk is reported as far too low for a
    backup, proving preflight was genuinely bypassed and not just quietly
    passing."""
    import bin.repair as repair
    from mcpbrain.store import Store

    monkeypatch.setattr(repair, "_free_bytes", lambda path: 1024)  # far below any backup's need
    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    for i in range(3):
        store.upsert_chunk(f"d{i}", "|  |  |", f"h{i}", {})

    out = _run("purge-empty", "--apply", "--skip-backup", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "backup written" not in out.stdout.lower(), out.stdout
    assert list(tmp_path.glob("brain.sqlite3.bak-*")) == []
    assert Store(tmp_path / "brain.sqlite3", dim=4).count_content_free() == 0, (
        "the purge itself must still run -- only the backup/preflight are skipped"
    )


def test_reingest_stale_wires_workers_into_reingest_files(tmp_path, monkeypatch):
    """--workers N must reach reingest_files as max_workers, with a
    service_factory only when N>1 (max_workers<=1 is the safe default that
    keeps every existing single-threaded caller/test unchanged)."""
    import bin.repair as repair
    from mcpbrain.store import Store

    calls = []

    def _fake_build_google_services():
        return {"drive_service": "fake-drive-service"}

    def _fake_reingest_files(service, store, ids, *, max_workers=1, service_factory=None, **kw):
        calls.append({"max_workers": max_workers, "service_factory": service_factory})
        return {"files": 0, "missing": 0, "failed": 0, "orphans": 0}

    monkeypatch.setattr("mcpbrain.auth.build_google_services", _fake_build_google_services)
    monkeypatch.setattr("mcpbrain.sync.drive.reingest_files", _fake_reingest_files)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_reingest_stale(store, True, limit=500, workers=5)
    repair.phase_reingest_stale(store, True, limit=500, workers=1)

    assert calls[0]["max_workers"] == 5
    assert calls[0]["service_factory"] is not None
    assert calls[1]["max_workers"] == 1
    assert calls[1]["service_factory"] is None


def test_reingest_stale_aggregates_skips_instead_of_writing_per_file(tmp_path, monkeypatch):
    """Review finding. Every other bulk Drive path passes a `report` dict and
    calls flush_skip_report once; reingest-stale passed neither, so
    fetch_content's _note_skip took its immediate-write branch — one
    store.record_change PER skipped file, from inside a WORKER THREAD.

    Two problems, both of which the report pattern exists to prevent:

    1. Writes escape the main thread. reingest_files deliberately confines every
       store write to _apply on the main thread (its own _reingest_one docstring
       says 'Never touches the store'), because the store is single-writer. The
       skip path was the one hole in that.
    2. change_log is pruned to 500 rows and doubles as the user-facing change
       digest. A 9,400-file re-ingest containing a few hundred images or
       unreadable files would evict the entire genuine audit trail and fill the
       digest with `ingest_skip: image/png` — exactly what _note_skip's own
       docstring says the aggregation was introduced to stop.
    """
    import bin.repair as repair
    from mcpbrain.store import Store

    seen: dict = {}

    def _fake_reingest_files(service, store, ids, *, report=None, **kw):
        seen["report"] = report
        return {"files": 0, "missing": 0, "failed": 0, "orphans": 0}

    flushed: list = []
    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"drive_service": "fake"})
    monkeypatch.setattr("mcpbrain.sync.drive.reingest_files", _fake_reingest_files)
    monkeypatch.setattr("mcpbrain.sync.drive.flush_skip_report",
                        lambda store, report, **kw: flushed.append(report))

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_reingest_stale(store, True, limit=10, workers=4)

    assert seen["report"] is not None, (
        "reingest-stale did not pass a report dict, so each skipped file writes "
        "change_log directly from a worker thread"
    )
    assert flushed and flushed[0] is seen["report"], (
        "the tally was collected but never flushed, so the skips vanish"
    )


def test_purge_apply_stops_when_a_batch_deletes_nothing(tmp_path, monkeypatch):
    """The purge loop re-selects until the selector comes back empty. A batch
    that selects rows but deletes none (a concurrent writer — the daemon —
    removing them between the select and the delete) would otherwise be
    re-selected forever: an infinite loop that writes nothing."""
    import bin.repair as repair

    class _SpinStore:
        def __init__(self):
            self.selects = 0

        def count_content_free(self):
            return 3

        def content_free_doc_ids(self, limit):
            self.selects += 1
            assert self.selects < 50, "phase_purge_empty spun on a no-progress batch"
            return ["d1", "d2", "d3"]

        def purge_doc_ids(self, doc_ids):
            return 0        # someone else deleted them first

    store = _SpinStore()

    rc = repair.phase_purge_empty(store, True)

    assert rc == 0
    assert store.selects == 1


def test_backfill_attachments_walks_years_backwards_and_resumes(tmp_path, monkeypatch):
    """A1's fix works on NEW mail via sync_gmail, but Gmail sync is delta-driven
    and never revisits history — measured live after spec 2/3 landed: 0
    email_attachment chunks in a 148k-chunk store. This phase is what makes the
    largest finding in the audit real for mail already in the mailbox.

    Resumability is the whole design: one year per invocation, newest first, with
    the last COMPLETED year in a cursor, so an interrupted run continues instead
    of restarting a full-history walk.
    """
    import bin.repair as repair
    from mcpbrain.store import Store

    calls: list = []

    def _fake_backfill(service, store, after, before=None, max_messages=None,
                       q_extra="", **kw):
        calls.append({"after": after, "before": before, "q_extra": q_extra})
        return 0  # under the limit -> the year counts as complete

    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"gmail_service": "fake"})
    monkeypatch.setattr("mcpbrain.sync.gmail.backfill_gmail", _fake_backfill)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_backfill_attachments(store, True, limit=500)
    first_year = int(calls[0]["after"][:4])
    assert calls[0]["q_extra"] == "has:attachment", (
        "the pass must narrow server-side, not re-walk the whole mailbox"
    )
    assert calls[0]["before"] == f"{first_year + 1}/01/01"

    repair.phase_backfill_attachments(store, True, limit=500)
    assert int(calls[1]["after"][:4]) == first_year - 1, (
        "the second run must move to the previous year, not repeat the first"
    )


def test_backfill_attachments_repeats_an_unfinished_year(tmp_path, monkeypatch):
    """Hitting the message limit means the year is NOT done, so the cursor must
    not advance past it — otherwise the tail of a heavy year is skipped forever."""
    import bin.repair as repair
    from mcpbrain.store import Store

    calls: list = []
    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"gmail_service": "fake"})
    monkeypatch.setattr(
        "mcpbrain.sync.gmail.backfill_gmail",
        lambda service, store, after, before=None, max_messages=None,
        q_extra="", **kw: calls.append(after) or max_messages)  # == limit

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_backfill_attachments(store, True, limit=10)
    repair.phase_backfill_attachments(store, True, limit=10)

    assert calls[0] == calls[1], f"cursor advanced past an unfinished year: {calls}"


def test_backfill_attachments_stops_at_the_floor(tmp_path, monkeypatch):
    import bin.repair as repair
    from mcpbrain.store import Store

    called: list = []
    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"gmail_service": "fake"})
    monkeypatch.setattr("mcpbrain.sync.gmail.backfill_gmail",
                        lambda *a, **kw: called.append(1) or 0)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.set_cursor(repair._ATT_CURSOR, "2008")

    repair.phase_backfill_attachments(store, True, limit=10, floor_year=2008)

    assert called == [], "walked past the floor year"


def test_backfill_attachments_dry_run_fetches_nothing(tmp_path, monkeypatch):
    import bin.repair as repair
    from mcpbrain.store import Store

    called: list = []
    monkeypatch.setattr("mcpbrain.sync.gmail.backfill_gmail",
                        lambda *a, **kw: called.append(1) or 0)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_backfill_attachments(store, False, limit=10)

    assert called == []
    assert store.get_cursor(repair._ATT_CURSOR) is None


def test_all_years_walks_the_whole_history_in_one_run(tmp_path, monkeypatch):
    """--all-years: 'run it at once' rather than one invocation per year. The
    per-year cursor still advances, so this stays resumable — the point is one
    command, not one giant unresumable window."""
    import bin.repair as repair
    from mcpbrain.store import Store

    years: list = []
    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"gmail_service": "fake"})
    monkeypatch.setattr("mcpbrain.sync.gmail.backfill_gmail",
                        lambda service, store, after, before=None, **kw:
                        years.append(int(after[:4])) or 0)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_backfill_attachments(store, True, limit=None, all_years=True,
                                      floor_year=2020)

    assert years == [2026, 2025, 2024, 2023, 2022, 2021, 2020], years
    assert store.get_cursor(repair._ATT_CURSOR) == "2020", (
        "the cursor must land on the floor year so a re-run is a no-op"
    )


def test_all_years_resumes_from_the_cursor(tmp_path, monkeypatch):
    import bin.repair as repair
    from mcpbrain.store import Store

    years: list = []
    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"gmail_service": "fake"})
    monkeypatch.setattr("mcpbrain.sync.gmail.backfill_gmail",
                        lambda service, store, after, before=None, **kw:
                        years.append(int(after[:4])) or 0)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.set_cursor(repair._ATT_CURSOR, "2024")   # 2024 already done

    repair.phase_backfill_attachments(store, True, limit=None, all_years=True,
                                      floor_year=2022)

    assert years == [2023, 2022], f"re-walked completed years: {years}"


def test_all_years_stops_at_a_year_that_hit_its_limit(tmp_path, monkeypatch):
    """A limited year is unfinished, so the walk must NOT move past it — otherwise
    --all-years would silently skip the tail of a heavy year."""
    import bin.repair as repair
    from mcpbrain.store import Store

    years: list = []
    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"gmail_service": "fake"})
    monkeypatch.setattr("mcpbrain.sync.gmail.backfill_gmail",
                        lambda service, store, after, before=None,
                        max_messages=None, **kw:
                        years.append(int(after[:4])) or max_messages)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_backfill_attachments(store, True, limit=10, all_years=True,
                                      floor_year=2020)

    assert years == [2026], f"walked past an unfinished year: {years}"
    assert store.get_cursor(repair._ATT_CURSOR) is None


def test_workers_and_a_service_factory_reach_backfill_gmail(tmp_path, monkeypatch):
    """Workers need their OWN Resource — googleapiclient's is not thread-safe."""
    import bin.repair as repair
    from mcpbrain.store import Store

    seen: dict = {}
    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"gmail_service": "fake"})
    monkeypatch.setattr("mcpbrain.sync.gmail.backfill_gmail",
                        lambda service, store, after, before=None,
                        max_workers=1, service_factory=None, **kw:
                        seen.update(w=max_workers, f=service_factory) or 0)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_backfill_attachments(store, True, limit=None, workers=8)
    assert seen["w"] == 8
    assert seen["f"] is not None

    repair.phase_backfill_attachments(store, True, limit=None, workers=1)
    assert seen["f"] is None, "no factory when sequential"


def test_limit_zero_means_no_limit(tmp_path):
    """`--limit 0 --all-years` is 'the entire mailbox, one run'. argparse cannot
    express None on an int, and 0 is meaningless as a real bound, so it is the
    natural sentinel."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    out = _run("backfill-attachments", "--limit", "0", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "limit none" in out.stdout, out.stdout


def test_embed_phase_drains_the_whole_backlog(tmp_path, monkeypatch):
    """Measured on the live store: the embedder does ~66 chunks/sec in isolation,
    but the daemon achieves ~5 — it gets only a slice of a cycle that is mostly
    sync, drain, prepare and cadences. So a large backlog (15,307 attachment
    chunks arrived at once) takes hours of daemon uptime for work the CPU can do
    in minutes.

    This phase is that work, done in one attended pass: no per-cycle budget, no
    item cap, one loop until nothing is pending."""
    import bin.repair as repair
    from mcpbrain.store import Store

    calls: list = []

    def _fake_index_pending(store, embedder, batch_size=32, **kw):
        calls.append(kw)
        # First call drains everything, second finds nothing (loop terminates).
        return 0 if len(calls) > 1 else 500

    monkeypatch.setattr("mcpbrain.index.index_pending", _fake_index_pending)
    monkeypatch.setattr("mcpbrain.embed.get_embedder", lambda *a, **kw: object())

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_embed_pending(store, True, limit=None)

    assert calls, "index_pending was never called"
    assert calls[0].get("budget") is None, (
        "a cycle budget would re-impose the daemon's own throttle"
    )
    assert calls[0].get("max_items") is None, "the point is to drain, not sample"


def test_embed_phase_dry_run_embeds_nothing(tmp_path, monkeypatch):
    import bin.repair as repair
    from mcpbrain.store import Store

    called: list = []
    monkeypatch.setattr("mcpbrain.index.index_pending",
                        lambda *a, **kw: called.append(1) or 0)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("d1", "some text", "h1", {})

    repair.phase_embed_pending(store, False, limit=None)

    assert called == []


def test_embed_phase_pauses_and_always_resumes_the_daemon(tmp_path, monkeypatch):
    """Two writers on one SQLite store is what the whole architecture avoids, so
    the drain pauses the daemon first. It MUST resume even if embedding raises —
    a repair that leaves the daemon paused has broken the user's brain to fix
    their backlog."""
    import bin.repair as repair
    from mcpbrain.store import Store

    events: list = []

    class _Client:
        def __init__(self, *a, **kw):
            pass

        def pause(self):
            events.append("pause")
            return {"status": "paused"}

        def resume(self):
            events.append("resume")
            return {"status": "running"}

    monkeypatch.setattr("mcpbrain.control_client.ControlClient", _Client)
    monkeypatch.setattr("mcpbrain.embed.get_embedder", lambda *a, **kw: object())

    def _boom(*a, **kw):
        events.append("embed")
        raise RuntimeError("onnx exploded")

    monkeypatch.setattr("mcpbrain.index.index_pending", _boom)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    with pytest.raises(RuntimeError):
        repair.phase_embed_pending(store, True, limit=None)

    assert events == ["pause", "embed", "resume"], events


def test_embed_phase_survives_an_unreachable_daemon(tmp_path, monkeypatch):
    """A stopped daemon means no contention at all — that is the easy case, not
    an error. It must not stop the drain."""
    import bin.repair as repair
    from mcpbrain.control_client import DaemonUnavailable
    from mcpbrain.store import Store

    def _unavailable(*a, **kw):
        raise DaemonUnavailable("not running")

    monkeypatch.setattr("mcpbrain.control_client.ControlClient", _unavailable)
    monkeypatch.setattr("mcpbrain.embed.get_embedder", lambda *a, **kw: object())
    drained: list = []
    monkeypatch.setattr("mcpbrain.index.index_pending",
                        lambda *a, **kw: drained.append(1) or 0)

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    repair.phase_embed_pending(store, True, limit=None)

    assert drained, "an unreachable daemon must not stop the drain"


def test_digest_provenance_patches_without_re_embedding(tmp_path):
    """The whole point: fix C2/C4 on stored digests with no model call, and
    without disturbing content_hash or embedded — otherwise "free" metadata
    repair silently re-queues 22k chunks for embedding."""
    import bin.repair as repair
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("enriched-cal-abc",
                       "[ACC] Email: Leaders Gathering\nFrom: \nDate: 2026-05-10\n",
                       "h1", {"source_type": "gmail_enriched_v2",
                              "thread_id": "cal-abc"})
    with store._connect() as db:
        rowid = db.execute("SELECT rowid FROM chunks WHERE doc_id=?",
                           ("enriched-cal-abc",)).fetchone()[0]
    store.write_embedding(rowid, [0.1, 0.2, 0.3, 0.4])
    before = store.get_chunk("enriched-cal-abc")

    repair.phase_digest_provenance(store, True, limit=None)

    after = store.get_chunk("enriched-cal-abc")
    assert after["metadata"]["date"] == "2026-05-10", "C2: date not recovered"
    assert after["metadata"]["source_type"] == "calendar_enriched_v2", "C4: label"
    assert after["content_hash"] == before["content_hash"], "content_hash changed"
    with store._connect() as db:
        assert db.execute("SELECT embedded FROM chunks WHERE doc_id=?",
                          ("enriched-cal-abc",)).fetchone()[0] == 1, (
            "the chunk was re-queued for embedding by a metadata patch")


def test_digest_provenance_is_idempotent(tmp_path):
    import bin.repair as repair
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("enriched-t1", "Date: 2026-05-10", "h1",
                       {"source_type": "gmail_enriched_v2", "thread_id": "t1"})

    repair.phase_digest_provenance(store, True, limit=None)
    first = store.get_chunk("enriched-t1")["metadata"]
    repair.phase_digest_provenance(store, True, limit=None)

    assert store.get_chunk("enriched-t1")["metadata"] == first


def test_digest_provenance_dry_run_writes_nothing(tmp_path):
    import bin.repair as repair
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("enriched-t1", "Date: 2026-05-10", "h1",
                       {"source_type": "gmail_enriched_v2", "thread_id": "t1"})

    repair.phase_digest_provenance(store, False, limit=None)

    assert "date" not in store.get_chunk("enriched-t1")["metadata"]


def test_digest_chunks_finds_mislabelled_rows(tmp_path):
    """Selection is on the doc_id prefix, not metadata.source_type — filtering on
    the field being corrected would skip exactly the rows that need it."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("enriched-cal-x", "t", "h1",
                       {"source_type": "gmail_enriched_v2", "thread_id": "cal-x"})
    store.upsert_chunk("gmail-m1-body-0", "t", "h2", {"source_type": "gmail"})

    got = store.digest_chunks()

    assert [d["doc_id"] for d in got] == ["enriched-cal-x"]


def test_digest_provenance_defaults_to_unbounded_from_the_cli(tmp_path):
    """digest-provenance is pure local metadata patching — no network call, no
    model call — so there is no cost reason to cap it at the shared 500 default.
    Silently inheriting that default would mean a user running
    `digest-provenance --apply` (the natural first command) believed they had
    fixed everything when only the first 500 (by rowid) were touched."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    for i in range(600):
        store.upsert_chunk(f"enriched-t{i}", f"Date: 2026-05-{(i % 28) + 1:02d}",
                           f"h{i}", {"source_type": "gmail_enriched_v2",
                                     "thread_id": f"t{i}"})

    out = _run("digest-provenance", "--apply", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "600" in out.stdout, out.stdout
    reopened = Store(tmp_path / "brain.sqlite3", dim=4)
    assert reopened.get_chunk("enriched-t599")["metadata"]["date"], (
        "the 600th digest was left unpatched — the shared --limit 500 default "
        "leaked into a phase that should be unbounded by default"
    )


def test_reingest_stale_still_defaults_to_500(tmp_path):
    """The opposite direction: reingest-stale spends real Drive API quota per
    file, so its default cap must be UNCHANGED by the digest-provenance/
    embed-pending carve-out."""
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()

    out = _run("reingest-stale", home=tmp_path)

    assert out.returncode == 0, out.stderr
    assert "limit 500" in out.stdout, out.stdout


def test_reingest_stale_dispatches_gmail_threads_through_reingest_messages(tmp_path, monkeypatch):
    """store.stale_chunker_ids (Task 7) returns gmail items too; this phase
    must route them to sync/gmail.py's reingest_messages (Task 8), not just
    Drive's reingest_files.

    Calls phase_reingest_stale directly (like the pre-existing
    test_reingest_stale_wires_workers_into_reingest_files /
    test_reingest_stale_aggregates_skips_instead_of_writing_per_file above) —
    _run() shells out to a subprocess, so a monkeypatch in this test process
    would never reach it.
    """
    import bin.repair as repair
    from mcpbrain.store import Store

    store = Store(tmp_path / "brain.sqlite3", dim=4)
    store.init()
    store.upsert_chunk("gmail-m1-body-0", "old", "h1",
                       {"source_type": "gmail", "thread_id": "t1",
                        "message_id": "m1", "chunker_version": 1})

    called = {}

    def _fake_reingest_messages(service, store, thread_ids, **kw):
        called["thread_ids"] = thread_ids
        return {"messages": 1, "missing": 0, "empty": 0, "failed": 0}

    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"gmail_service": "fake-gmail-service"})
    monkeypatch.setattr("mcpbrain.sync.gmail.reingest_messages",
                        _fake_reingest_messages)

    rc = repair.phase_reingest_stale(store, True, limit=500, workers=1)

    assert rc == 0
    assert called.get("thread_ids") == ["t1"]
