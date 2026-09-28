"""The reflow seed cadence: tops the reflow sync_queue up to REFLOW_WINDOW,
gated on the kill switch, the halt flag, and a backup that succeeded within
the last 24h.

Store.reflow_candidates/reflow_stats/enqueue_items and the selector rules are
unit 1d's (mcpbrain/store.py) and already tested in tests/test_reflow_selector.py
-- this file only covers the daemon cadence built on top of them.
"""
import json
import time

from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


_ALL_SERVICES = {"gmail_service": object(), "drive_service": object(),
                 "calendar_service": object()}


def _c(s, doc_id, **md):
    s.upsert_chunk(doc_id, "t " + doc_id, doc_id, md)


def test_seed_requires_recent_backup_and_tops_up_window(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    d = dmod.Daemon.__new__(dmod.Daemon)          # minimal instance, as other cadence tests do
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = _ALL_SERVICES, True
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() == {"reflow_seed": "no_recent_backup"}
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d._last_reflow_seed = None
    assert d._run_reflow_seed()["enqueued"] == 1


def test_seed_registered_and_defaults(tmp_path):
    from mcpbrain import daemon as dmod
    assert "reflow_seed" in {cp.name for cp in dmod._CADENCE_PASSES}
    assert dmod._CADENCE_DEFAULTS["reflow_seed_interval_s"] == 3600.0
    assert "reflow_seed_interval_s" in dmod._CADENCE_KEYS
    assert dmod._cadences_from_config(str(tmp_path))["reflow_seed_interval_s"] == 3600.0


def test_seed_not_due_returns_none(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = None, None
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() is None


def test_seed_disabled_by_kill_switch(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = _ALL_SERVICES, True
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(dmod.config, "reflow_enabled", lambda home: False)
    assert d._run_reflow_seed() == {"reflow_seed": "disabled"}


def test_seed_halted(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = _ALL_SERVICES, True
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() == {"reflow_seed": "halted"}


def test_seed_stale_backup_still_gates(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    stale = time.time() - dmod.REFLOW_BACKUP_MAX_AGE_S - 10
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": stale}))
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = _ALL_SERVICES, True
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() == {"reflow_seed": "no_recent_backup"}


def test_seed_window_full_reports_zero_enqueued(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    items = [{"ref_id": f"F{i}", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}
             for i in range(dmod.REFLOW_WINDOW)]
    s.enqueue_items(items, source="reflow:drive")
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = _ALL_SERVICES, True
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    assert d._run_reflow_seed() == {"reflow_seed": "window_full", "enqueued": 0}


def test_seed_backlog_empty_runs_integrity_check_once(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = _ALL_SERVICES, True
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    calls = []

    def _fake_check(home):
        calls.append(home)
        return []

    import mcpbrain.doctor as doctor_mod
    monkeypatch.setattr(doctor_mod, "_run_integrity_check", _fake_check)
    out = d._run_reflow_seed()
    assert out == {"reflow_seed": "ok", "enqueued": 0}
    assert calls == [str(tmp_path)]
    assert s.get_cursor("reflow:integrity_checked") == "ok"

    # Second call must not re-run the check.
    d._last_reflow_seed = None
    d._run_reflow_seed()
    assert calls == [str(tmp_path)]


def _seed_daemon(tmp_path, monkeypatch, s):
    from mcpbrain import daemon as dmod
    d = dmod.Daemon.__new__(dmod.Daemon)
    d._store, d._clock = s, time.monotonic
    d._reflow_seed_interval_s, d._last_reflow_seed = 1.0, None
    d._services, d._services_resolved = {"gmail_service": object(), "drive_service": object(),
                                         "calendar_service": object()}, True
    monkeypatch.setattr(dmod, "app_dir", lambda: tmp_path)
    return d


def test_seed_persists_its_last_status(tmp_path, monkeypatch):
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    d = _seed_daemon(tmp_path, monkeypatch, s)
    d._run_reflow_seed()
    assert json.loads(s.get_cursor("reflow:last_seed"))["status"] == "no_recent_backup"
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d._last_reflow_seed = None
    d._run_reflow_seed()
    last = json.loads(s.get_cursor("reflow:last_seed"))
    assert last["status"] == "ok" and last["enqueued"] == 1


def test_seed_enqueue_resets_the_integrity_marker(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.set_cursor("reflow:integrity_checked", "ok")
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _seed_daemon(tmp_path, monkeypatch, s)
    assert d._run_reflow_seed()["enqueued"] == 1
    assert not s.get_cursor("reflow:integrity_checked")


# -- final review I6: a permanently unavailable source is not seeded ----------

def test_seed_skips_sources_without_a_service_and_frees_their_queued_rows(tmp_path, monkeypatch):
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    _c(s, "cal-E-0", source_type="calendar", event_id="E", chunk_total=2)
    _c(s, "anarlog-S-transcript-0", source_type="anarlog", session_id="S", chunk_total=2)
    s.enqueue_items([{"ref_id": "OLD", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:calendar")
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _seed_daemon(tmp_path, monkeypatch, s)
    d._services = {"gmail_service": object()}           # no calendar scope granted
    out = d._run_reflow_seed()
    assert out["enqueued"] == 1
    with s._connect() as db:
        rows = {(r[0], r[1]) for r in db.execute("SELECT source, ref_id FROM sync_queue")}
    # anarlog disabled (not configured) -> not seeded; calendar -> not seeded,
    # and its queued row no longer holds the window
    assert rows == {("reflow:gmail", "N")}


def test_seed_seeds_anarlog_when_enabled(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    _c(s, "anarlog-S-transcript-0", source_type="anarlog", session_id="S", chunk_total=2)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _seed_daemon(tmp_path, monkeypatch, s)
    monkeypatch.setattr(dmod.config, "anarlog_db_path", lambda home: tmp_path / "anarlog.db")
    assert d._run_reflow_seed()["enqueued"] == 1


def test_selector_can_be_restricted_to_sources(tmp_path):
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    _c(s, "cal-E-0", source_type="calendar", event_id="E", chunk_total=2)
    assert s.reflow_candidates(50, sources={"reflow:calendar"}) == [("reflow:calendar", "E")]


def test_end_to_end_seed_work_queue_handler_converges(tmp_path, monkeypatch):
    """seed -> sync_queue -> work_queue -> ReflowContext -> apply_reflow, on a
    real Store with fake services: the selector ends empty, enrichment and a
    relation survive, and the next seed runs the integrity check once."""
    from mcpbrain.sync import drive, queue
    from mcpbrain.sync.blocks import Heading, Paragraph
    from mcpbrain.sync.reflow_handler import ReflowContext
    import mcpbrain.doctor as doctor_mod
    PDF = "application/pdf"
    M = "2026-01-01T00:00:00Z"
    s = _store(tmp_path)
    for fid in ("F", "G"):
        for i, t in enumerate(("Budget Line one", "Line two")):
            s.upsert_chunk(f"gdrive-{fid}-{i}", t, f"{fid}{i}",
                           {"source_type": "gdrive", "file_id": fid, "mime_type": PDF,
                            "modified": M, "chunk_index": i, "chunk_total": 2})
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET enriched=1")
        db.execute("INSERT INTO entities(id, name, type) VALUES('e1','Priya Anand','person')")
        db.execute("INSERT INTO entities(id, name, type) VALUES('e2','Northgate Trust','org')")
        db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, source_doc_id)"
                   " VALUES('e1','works_at','e2','gdrive-F-1')")
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _seed_daemon(tmp_path, monkeypatch, s)
    assert d._run_reflow_seed() == {"reflow_seed": "ok", "enqueued": 2}

    monkeypatch.setattr(drive, "fetch_content", lambda svc, fm, **k: drive.Content(
        text="Budget\n\nLine one\nLine two",
        blocks=[Heading(1, "Budget"), Paragraph("Line one\nLine two")]))
    monkeypatch.setattr(drive, "folder_path", lambda *a, **k: "")

    class _Svc:
        def files(self):
            return self

        def get(self, **kw):
            class R:
                def execute(self, num_retries=0):
                    return {"id": kw["fileId"], "name": "r.pdf", "mimeType": PDF,
                            "modifiedTime": M, "parents": []}
            return R()

    class _Emb:
        dim = 4

        def embed_passages(self, xs):
            return [[0.1, 0.2, 0.3, 0.4] for _ in xs]
    ctx = ReflowContext(s, _Emb(), str(tmp_path), drive_service=_Svc())
    assert queue.work_queue(s, handlers={"reflow": ctx.handle}, limit=10) == {
        "processed": 2, "failed": 0}
    assert s.reflow_candidates(50) == []
    st = s.reflow_stats(live_remaining=True)
    assert st["by_outcome"] == {"carried": 2} and st["queued"] == 0 and st["remaining"] == 0
    assert all(r["enriched"] == 1 for r in s.owner_chunks(["gdrive-F-", "gdrive-G-"]))
    with s._connect() as db:
        assert db.execute("SELECT source_doc_id FROM entity_relations").fetchone()[0] == "gdrive-F-0"

    calls = []
    monkeypatch.setattr(doctor_mod, "_run_integrity_check", lambda home: calls.append(home) or [])
    d._last_reflow_seed = None
    assert d._run_reflow_seed() == {"reflow_seed": "ok", "enqueued": 0}
    assert calls and s.get_cursor("reflow:integrity_checked") == "ok"
    assert doctor_mod.reflow_line(s).startswith("✅")


# -- residual R3: every seed branch records remaining (never reads as idle) ---

def _blocked_daemon(tmp_path, monkeypatch, s):
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    return _seed_daemon(tmp_path, monkeypatch, s)


def test_disabled_seed_records_remaining(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    d = _blocked_daemon(tmp_path, monkeypatch, s)
    monkeypatch.setattr(dmod.config, "reflow_enabled", lambda home: False)
    assert d._run_reflow_seed() == {"reflow_seed": "disabled"}
    last = json.loads(s.get_cursor("reflow:last_seed"))
    assert last["status"] == "disabled" and last["remaining"] == 1


def test_halted_seed_records_remaining(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    d = _blocked_daemon(tmp_path, monkeypatch, s)
    d._run_reflow_seed()
    last = json.loads(s.get_cursor("reflow:last_seed"))
    assert last["status"] == "halted" and last["remaining"] == 1


def test_error_seed_records_remaining(tmp_path, monkeypatch):
    s = _store(tmp_path)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _blocked_daemon(tmp_path, monkeypatch, s)

    def boom(*_a, **_k):
        raise RuntimeError("disk full")

    monkeypatch.setattr(s, "enqueue_items", boom)
    assert d._run_reflow_seed()["reflow_seed"] is False
    last = json.loads(s.get_cursor("reflow:last_seed"))
    assert last["status"] == "error" and last["remaining"] == 1
    assert "disk full" in last["error"]


# -- residual R4: live remaining is restricted to the sources the seed works --

def test_live_remaining_uses_the_seeds_workable_sources(tmp_path, monkeypatch):
    """No calendar scope granted: the calendar owner is never seedable, so it
    must not keep doctor's remaining above zero forever."""
    from mcpbrain.doctor import reflow_line
    s = _store(tmp_path)
    _c(s, "cal-E-0", source_type="calendar", event_id="E", chunk_total=2)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _seed_daemon(tmp_path, monkeypatch, s)
    d._services = {"gmail_service": object(), "drive_service": object()}
    monkeypatch.setattr("mcpbrain.doctor._run_integrity_check", lambda home: [])
    assert d._run_reflow_seed()["enqueued"] == 0
    assert s.reflow_stats(live_remaining=True)["remaining"] == 0
    assert reflow_line(s).startswith("✅")


# -- residual R5: a transient service failure never frees queued rows --------

_SCOPES = ["https://www.googleapis.com/auth/gmail.readonly",
           "https://www.googleapis.com/auth/calendar.readonly",
           "https://www.googleapis.com/auth/drive.readonly"]


def _queued(s):
    with s._connect() as db:
        return {(r[0], r[1], r[2]) for r in db.execute(
            "SELECT source, ref_id, attempts FROM sync_queue")}


def test_transient_service_failure_leaves_queued_rows(tmp_path, monkeypatch):
    s = _store(tmp_path)
    _c(s, "cal-E-0", source_type="calendar", event_id="E", chunk_total=2)
    s.enqueue_items([{"ref_id": "OLD", "event": "reflow",
                      "modified_at": "1970-01-01T00:00:00"}], source="reflow:calendar")
    with s._connect(write=True) as db:
        db.execute("UPDATE sync_queue SET attempts=2 WHERE ref_id='OLD'")
    (tmp_path / "google_token.json").write_text(json.dumps({"scopes": _SCOPES}))
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _seed_daemon(tmp_path, monkeypatch, s)
    d._services = {}                          # ensure_services could not build them now
    out = d._run_reflow_seed()
    assert out["enqueued"] == 0               # nothing seeded it cannot work now ...
    assert _queued(s) == {("reflow:calendar", "OLD", 2)}   # ... and nothing freed
    last = json.loads(s.get_cursor("reflow:last_seed"))
    assert "reflow:calendar" in last["sources"]


def test_scope_not_granted_frees_queued_rows(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.enqueue_items([{"ref_id": "OLD", "event": "reflow",
                      "modified_at": "1970-01-01T00:00:00"}], source="reflow:calendar")
    (tmp_path / "google_token.json").write_text(json.dumps({"scopes": _SCOPES[:1]}))
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _seed_daemon(tmp_path, monkeypatch, s)
    d._services = {"gmail_service": object()}
    d._run_reflow_seed()
    assert _queued(s) == set()
    last = json.loads(s.get_cursor("reflow:last_seed"))
    assert "reflow:calendar" not in last["sources"]


# -- hardening H1: a source-resolution failure never frees queued rows -------

def test_source_resolution_error_returns_before_dropping_or_completing(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.enqueue_items([{"ref_id": "OLD", "event": "reflow",
                      "modified_at": "1970-01-01T00:00:00"}], source="reflow:drive")
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    d = _seed_daemon(tmp_path, monkeypatch, s)

    def boom(home):
        raise RuntimeError("token unreadable")

    monkeypatch.setattr(d, "_reflow_source_sets", boom)
    checked = []
    monkeypatch.setattr("mcpbrain.doctor._run_integrity_check",
                        lambda home: checked.append(home) or [])
    out = d._run_reflow_seed()
    assert out["reflow_seed"] is False and "token unreadable" in out["error"]
    assert _queued(s) == {("reflow:drive", "OLD", 0)}      # nothing freed
    assert checked == []                                   # never "backlog empty"
    last = json.loads(s.get_cursor("reflow:last_seed"))
    assert last["status"] == "error" and "token unreadable" in last["error"]


# -- hardening H6: disabled/halted ticks reuse the last known remaining -------

def _no_scan(monkeypatch, s):
    calls = []
    real = s.reflow_candidates
    monkeypatch.setattr(s, "reflow_candidates",
                        lambda *a, **k: calls.append(a) or real(*a, **k))
    return calls


def test_disabled_seed_does_not_rescan_candidates(tmp_path, monkeypatch):
    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    d = _blocked_daemon(tmp_path, monkeypatch, s)
    monkeypatch.setattr(dmod.config, "reflow_enabled", lambda home: False)
    s.set_cursor("reflow:last_seed", json.dumps({"status": "ok", "remaining": 7}))
    calls = _no_scan(monkeypatch, s)
    assert d._run_reflow_seed() == {"reflow_seed": "disabled"}
    assert calls == []
    last = json.loads(s.get_cursor("reflow:last_seed"))
    assert last["status"] == "disabled" and last["remaining"] == 7


def test_halted_seed_does_not_rescan_candidates(tmp_path, monkeypatch):
    s = _store(tmp_path)
    s.set_cursor("reflow:halted", "reflow F: 1 dangling reference(s)")
    d = _blocked_daemon(tmp_path, monkeypatch, s)
    s.set_cursor("reflow:last_seed", json.dumps({"status": "halted", "remaining": 3}))
    calls = _no_scan(monkeypatch, s)
    d._run_reflow_seed()
    assert calls == []
    assert json.loads(s.get_cursor("reflow:last_seed"))["remaining"] == 3


def test_deferred_rows_do_not_hold_the_window_full(tmp_path, monkeypatch):
    """Task 11/12 triage: rows a handler deferred with a delay (a long transient
    outage) are not due, so they must not count toward REFLOW_WINDOW -- else
    the window stays full and every other source's reflow stalls."""
    from datetime import datetime, timedelta, timezone

    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    items = [{"ref_id": f"F{i}", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}
             for i in range(dmod.REFLOW_WINDOW)]
    s.enqueue_items(items, source="reflow:drive")
    later = (datetime.now(timezone.utc).replace(tzinfo=None)
             + timedelta(hours=1)).isoformat()
    for it in items:
        assert s.defer_sync_item("reflow:drive", it["ref_id"], later)
    d = _seed_daemon(tmp_path, monkeypatch, s)
    out = d._run_reflow_seed()
    assert out["reflow_seed"] == "ok" and out["enqueued"] == 1


def test_due_rows_still_fill_the_window(tmp_path, monkeypatch):
    from datetime import datetime, timedelta, timezone

    from mcpbrain import daemon as dmod
    s = _store(tmp_path)
    _c(s, "gmail-N-body-0", source_type="gmail", message_id="N", chunk_total=2)
    (tmp_path / "backup_state.json").write_text(json.dumps({"last_success": time.time()}))
    items = [{"ref_id": f"F{i}", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}
             for i in range(dmod.REFLOW_WINDOW)]
    s.enqueue_items(items, source="reflow:drive")
    past = (datetime.now(timezone.utc).replace(tzinfo=None)
            - timedelta(minutes=1)).isoformat()
    for it in items[:10]:
        s.defer_sync_item("reflow:drive", it["ref_id"], past)   # delay elapsed: due
    d = _seed_daemon(tmp_path, monkeypatch, s)
    assert d._run_reflow_seed()["reflow_seed"] == "window_full"
