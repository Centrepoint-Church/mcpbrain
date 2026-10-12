"""A TRANSIENT failure to reach Google at daemon start must not disable sync,
backup or the org-config merge for the whole run.

Live 2026-10-09: the daemon was bootstrapped while oauth2.googleapis.com was
briefly unreachable (Errno 49, local ephemeral ports exhausted by a drain).
Services, backup and the org-config merge each resolved "permanently" to
nothing on that one failure, and stayed off for three days until a manual
restart. Now a transient failure retries on a backoff (60 s, doubling, capped
at 900 s); a permanent one (no token, revoked grant) behaves as before.

Fully offline: auth.build_google_services and _build_drive_service are
monkeypatched, and the clocks are injected.
"""
import errno
import logging

import pytest
from google.auth import exceptions as gae

from mcpbrain import auth
from mcpbrain import daemon as dmod
from mcpbrain.daemon import Daemon
from mcpbrain.store import Store


class FakeEmbedder:
    dim = 4

    def embed(self, texts):
        return [[0.0] * 4 for _ in texts]


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _transient():
    # The live shape: google-auth wraps the requests ConnectionError.
    return gae.TransportError(OSError(errno.EADDRNOTAVAIL, "Can't assign requested address"))


class FlakyBuild:
    """build_google_services stand-in: raises each queued exception in turn,
    then returns `result`. Counts calls."""

    def __init__(self, *errors, result=None):
        self.errors = list(errors)
        self.result = result if result is not None else {}
        self.calls = 0

    def __call__(self, *a, **kw):
        self.calls += 1
        if self.errors:
            raise self.errors.pop(0)
        return self.result


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    return tmp_path


def _daemon(home, clock, wall=None):
    store = Store(home / "b.sqlite3", dim=4)
    store.init()
    return Daemon(store, FakeEmbedder(), clock=clock,
                  wall_clock=wall or (lambda: 1_789_000_000.0))


# -- services -----------------------------------------------------------------

def test_transient_failure_retries_on_backoff_then_restores(home, monkeypatch, caplog):
    clock = Clock()
    gmail, drive = object(), object()
    build = FlakyBuild(_transient(), result={"gmail_service": gmail, "drive_service": drive})
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, clock)

    with caplog.at_level(logging.INFO, logger="mcpbrain.daemon"):
        assert d.ensure_services() == {}
        assert build.calls == 1
        assert d._services_resolved is False
        assert d._google_services_pending()

        # Before retry_at: cheap {} and auth is NOT called again.
        clock.t += 59
        assert d.ensure_services() == {}
        assert build.calls == 1

        # After retry_at: rebuilt, resolved, recovery logged once.
        clock.t += 1
        assert d.ensure_services() == {"gmail_service": gmail, "drive_service": drive}
        assert build.calls == 2
        assert d._services_resolved is True
        d.ensure_services()
        assert build.calls == 2

    restored = [r for r in caplog.records if "Google services restored" in r.getMessage()]
    assert len(restored) == 1
    assert "after 2 attempt(s)" in restored[0].getMessage()
    assert any("transient" in r.getMessage() and "will retry" in r.getMessage()
               for r in caplog.records)
    assert d.google_unavailable() is None


@pytest.mark.parametrize("exc", [
    FileNotFoundError("google_token.json"),
    gae.RefreshError("invalid_grant: Token has been expired or revoked."),
    RuntimeError("No valid credentials found"),
])
def test_permanent_failure_resolves_empty_without_retry(home, monkeypatch, exc):
    clock = Clock()
    build = FlakyBuild(exc)
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, clock)

    assert d.ensure_services() == {}
    assert d._services_resolved is True
    clock.t += 10_000
    assert d.ensure_services() == {}
    assert build.calls == 1
    assert d.google_unavailable() is None
    assert not d._google_services_pending()


def test_injected_services_never_call_auth(home, monkeypatch):
    build = FlakyBuild(_transient())
    monkeypatch.setattr(auth, "build_google_services", build)
    store = Store(home / "b.sqlite3", dim=4)
    store.init()
    d = Daemon(store, FakeEmbedder(), services={})
    assert d.ensure_services() == {}
    assert build.calls == 0


def test_first_time_success_is_unchanged(home, monkeypatch, caplog):
    gmail = object()
    build = FlakyBuild(result={"gmail_service": gmail})
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, Clock())
    with caplog.at_level(logging.INFO, logger="mcpbrain.daemon"):
        assert d.ensure_services() == {"gmail_service": gmail}
        d.ensure_services()
    assert build.calls == 1
    assert not any("restored" in r.getMessage() for r in caplog.records)


def test_org_pass_stays_due_while_services_await_retry(home, monkeypatch):
    """A daily org pass that ran with no Drive service during the outage must
    not stamp itself done for a day: it stays due for the tick after recovery."""
    clock = Clock()
    monkeypatch.setattr(auth, "build_google_services", FlakyBuild(_transient()))
    monkeypatch.setattr(dmod.config, "org_import_enabled", lambda home: True)
    d = _daemon(home, clock)
    d._org_import_interval_s = 86400.0
    d._last_org_import = None
    d.ensure_services()
    assert d._run_org_import() == {"skipped": "no_fleet_storage"}
    assert d._last_org_import is None


# -- backoff ------------------------------------------------------------------

def test_backoff_doubles_and_caps_at_900s():
    clock = Clock(0.0)
    bo = dmod._GoogleBackoff("x", clock, lambda: 0.0)
    delays = []
    for _ in range(7):
        bo.failed(_transient())
        delays.append(bo.retry_at - clock.t)
    assert delays == [60.0, 120.0, 240.0, 480.0, 900.0, 900.0, 900.0]
    assert dmod.GOOGLE_RETRY_MAX_S == 900.0
    assert not bo.due()
    clock.t += 900
    assert bo.due()


# -- backup -------------------------------------------------------------------

def _configure_backup(home):
    from mcpbrain import config
    config.write_config(str(home), {"backup": {
        "escrow_key": "k" * 44, "shared_drive_id": "SD", "user_id": "u1"}})


def test_backup_transient_build_failure_rebuilds_later_and_backs_up(home, monkeypatch):
    _configure_backup(home)
    clock = Clock()
    drive = object()
    build = FlakyBuild(_transient(), result={"drive_service": drive})
    monkeypatch.setattr(auth, "build_google_services", build)

    # Start-up, as main() does it.
    res = dmod._backup_from_config(str(home))
    assert tuple(res) == (None, None)
    assert res.transient is not None
    d = _daemon(home, clock)
    d._backup_backoff.failed(res.transient)
    assert d.google_unavailable()["affects"] == ["backup"]

    d._retry_backup_build()                 # not due yet
    assert d._backup is None and build.calls == 1

    clock.t += 60
    d._retry_backup_build()
    assert build.calls == 2
    assert d._backup is not None and d._backup.drive_service is drive
    assert d._backup_interval_s == dmod.DEFAULT_BACKUP_INTERVAL_S
    assert d.google_unavailable() is None

    # ... and a backup is now actually possible.
    uploaded = []
    monkeypatch.setattr(dmod, "_mac_user_is_active", lambda: True)
    monkeypatch.setattr(dmod, "make_encrypted_snapshot", lambda *a, **k: home / "snap.enc")
    monkeypatch.setattr(dmod, "upload_snapshot",
                        lambda svc, path, sd, uid: uploaded.append(svc) or "FILE1")
    monkeypatch.setattr("mcpbrain.backup.prune_snapshots", lambda *a, **k: None)
    out = d.maybe_backup()
    assert out["backed_up"] is True
    assert uploaded == [drive]


def test_backup_permanent_build_failure_stays_disabled(home, monkeypatch):
    _configure_backup(home)
    clock = Clock()
    build = FlakyBuild(FileNotFoundError("google_token.json"))
    monkeypatch.setattr(auth, "build_google_services", build)

    res = dmod._backup_from_config(str(home))
    assert tuple(res) == (None, None)
    assert res.transient is None
    d = _daemon(home, clock)
    clock.t += 10_000
    d._retry_backup_build()
    assert d._backup is None
    assert build.calls == 1
    assert d.maybe_backup() is None


def test_apply_config_transient_backup_failure_schedules_retry(home, monkeypatch):
    clock = Clock()
    drive = object()
    build = FlakyBuild(_transient(), result={"drive_service": drive})
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, clock)
    d.apply_config({"backup": {"escrow_key": "k" * 44, "shared_drive_id": "SD",
                               "user_id": "u1"}})
    assert d._backup is None
    assert d._backup_backoff.pending
    clock.t += 60
    d._retry_google_builds()
    assert d._backup is not None and d._backup.drive_service is drive


# -- org-config merge ---------------------------------------------------------

def test_org_config_merge_transient_retries(home, monkeypatch):
    (home / "google_token.json").write_text("{}")
    monkeypatch.setattr(auth, "token_path", lambda: home / "google_token.json")
    clock = Clock()
    merges = []
    build = FlakyBuild(_transient(), result="SVC")
    monkeypatch.setattr(dmod, "_build_drive_service", build)
    monkeypatch.setattr("mcpbrain.fleet.merge_org_config",
                        lambda home, svc: merges.append(svc))

    exc = dmod._maybe_merge_org_config(str(home))
    assert exc is not None and merges == []
    d = _daemon(home, clock)
    d._org_config_backoff.failed(exc)

    d._retry_google_builds()                # not due
    assert merges == []
    clock.t += 60
    d._retry_google_builds()
    assert merges == ["SVC"]
    assert not d._org_config_backoff.pending


def test_org_config_merge_permanent_is_skipped_not_retried(home, monkeypatch):
    (home / "google_token.json").write_text("{}")
    monkeypatch.setattr(auth, "token_path", lambda: home / "google_token.json")
    build = FlakyBuild(RuntimeError("No valid credentials found"))
    monkeypatch.setattr(dmod, "_build_drive_service", build)
    assert dmod._maybe_merge_org_config(str(home)) is None
    assert build.calls == 1


# -- visibility ---------------------------------------------------------------

def test_status_reports_google_unavailable_then_null_after_recovery(home, monkeypatch):
    clock = Clock()
    build = FlakyBuild(_transient(), _transient(), result={"gmail_service": object()})
    monkeypatch.setattr(auth, "build_google_services", build)
    d = _daemon(home, clock, wall=lambda: 1_789_000_000.0)

    d.ensure_services()
    clock.t += 60
    d.ensure_services()
    gu = d.status()["google_unavailable"]
    assert gu["attempts"] == 2
    assert gu["since"] == "2026-09-10T00:26:40Z"
    assert "Can't assign requested address" in gu["last_error"]
    assert len(gu["last_error"]) <= 200
    assert gu["affects"] == ["sync services"]

    clock.t += 120
    d.ensure_services()
    assert d.status()["google_unavailable"] is None


def test_doctor_line_appears_while_google_unavailable():
    from mcpbrain import doctor
    line = doctor.google_unavailable_line({"google_unavailable": {
        "since": "2026-10-09T08:00:00Z", "attempts": 5,
        "last_error": "Max retries exceeded", "affects": ["sync services", "backup"]}})
    assert line.startswith("⚠️")
    assert "unreachable since 2026-10-09T08:00:00Z" in line
    assert "retrying (5 attempts)" in line
    assert doctor.google_unavailable_line({"google_unavailable": None}) is None
    assert doctor.google_unavailable_line(None) is None


# -- restore-on-first-run vs a transient outage: HOLD, never overwrite ---------

class AdvancingStop:
    """_stop stand-in whose wait() advances the injected clock instead of sleeping."""

    def __init__(self, clock):
        self.clock, self.waits, self._set = clock, [], False

    def is_set(self):
        return self._set

    def set(self):
        self._set = True

    def wait(self, s):
        self.waits.append(s)
        self.clock.t += s
        return self._set


def _restore_rig(home, monkeypatch, *, snapshot="SNAP1", find_errors=(), build_errors=(),
                 restore_error=None):
    """Backup config + Drive stand-ins. Returns a dict of call records."""
    _configure_backup(home)
    rec = {"restored": [], "uploaded": [], "pruned": [], "gates": []}
    build = FlakyBuild(*build_errors, result={"drive_service": "DRIVE"})
    monkeypatch.setattr(auth, "build_google_services", build)
    rec["build"] = build
    errs = list(find_errors)

    def find(svc, sd, uid):
        if errs:
            raise errs.pop(0)
        return snapshot

    def restore(bc, store, fid, **kw):
        rec["gates"].append(kw.get("gate"))
        if restore_error is not None:
            raise restore_error
        rec["restored"].append(fid)
    monkeypatch.setattr(dmod.backup, "find_latest_snapshot", find)
    monkeypatch.setattr(dmod.backup, "download_and_restore", restore)
    monkeypatch.setattr(dmod, "_mac_user_is_active", lambda: True)
    monkeypatch.setattr(dmod, "make_encrypted_snapshot", lambda *a, **k: home / "snap.enc")
    monkeypatch.setattr(dmod, "upload_snapshot",
                        lambda svc, path, sd, uid: rec["uploaded"].append(path) or "F")
    monkeypatch.setattr("mcpbrain.backup.prune_snapshots",
                        lambda *a, **k: rec["pruned"].append(1))
    return rec


def _held_daemon(home, monkeypatch, clock, **rig):
    """A daemon that started on an empty store during an outage and gave up
    the start-up wait at once: the restore hold is on."""
    rec = _restore_rig(home, monkeypatch, **rig)
    d = _daemon(home, clock)
    d._restore_wait_s = 0
    d._startup_restore()
    assert d._restore_pending is True
    return d, rec


def test_hold_blocks_sync_passes_and_backup(home, monkeypatch):
    clock = Clock()
    d, rec = _held_daemon(home, monkeypatch, clock, build_errors=[_transient()])
    assert rec["restored"] == []
    assert dmod._read_restore_pending(str(home)) is not None

    # Google comes back: services and the backup config recover ...
    clock.t += 60
    assert d.ensure_services() != {}
    d._backup_backoff.failed(_transient())
    clock.t += 60
    d._retry_backup_build()
    assert d._backup is not None
    # ... but no sync cycle runs ...
    cycles = []
    monkeypatch.setattr(dmod, "run_cycle", lambda *a, **k: cycles.append(1) or {})
    assert d.run_one() is None
    assert cycles == []
    # ... no content-writing maintenance pass runs (a hold-safe one does) ...
    ran = []
    monkeypatch.setattr(d, "_run_feedback_aggregate", lambda: ran.append("feedback"))
    monkeypatch.setattr(d, "_run_auto_update", lambda: ran.append("auto_update"))
    d._run_periodic_passes()
    assert "auto_update" in ran and "feedback" not in ran
    # ... and nothing is uploaded or pruned.
    assert d.maybe_backup() == {"backed_up": False, "skipped": "restore_pending"}
    assert rec["uploaded"] == [] and rec["pruned"] == []
    assert d._store.chunk_count() == 0
    st = d.status()["restore_pending"]
    assert st["since"] and st["restart_budget_spent"] is False


def test_reachable_snapshot_restarts_into_the_empty_store_restore(home, monkeypatch):
    clock = Clock()
    d, rec = _held_daemon(home, monkeypatch, clock, find_errors=[_transient()])
    restarts = []
    monkeypatch.setattr(d, "_restart_process", lambda *a: restarts.append(1))
    monkeypatch.setattr(d, "_watchdog_may_exit", lambda: True)
    clock.t += 30
    d._retry_google_builds()                 # not due yet
    assert restarts == []
    clock.t += 30
    d._retry_google_builds()
    assert restarts == [1]
    assert rec["restored"] == []             # never in place, under live writers

    # The "restarted" process: the store stayed empty, so the ordinary
    # empty-store restore runs, through the restoring gate, and clears the hold.
    d2 = _daemon(home, Clock())
    d2._startup_restore()
    assert rec["restored"] == ["SNAP1"]
    assert rec["gates"][-1] is not None
    assert d2._restore_pending is False
    assert dmod._read_restore_pending(str(home)) is None


def test_marker_with_content_in_the_store_is_never_overwritten(home, monkeypatch):
    rec = _restore_rig(home, monkeypatch)
    dmod._write_restore_pending(str(home), "earlier outage")
    d = _daemon(home, Clock())
    d._store.upsert_chunk(doc_id="d", text="x", content_hash="h", metadata={})
    d._startup_restore()
    assert rec["restored"] == []
    assert d._store.chunk_count() == 1


def test_budget_spent_stays_held_and_doctor_says_so(home, monkeypatch):
    clock = Clock()
    d, rec = _held_daemon(home, monkeypatch, clock, find_errors=[_transient()])
    restarts = []
    monkeypatch.setattr(d, "_restart_process", lambda *a: restarts.append(1))
    monkeypatch.setattr(d, "_watchdog_may_exit", lambda: False)
    clock.t += 60
    d._retry_google_builds()
    assert restarts == [] and d._restore_pending is True
    st = d.status()
    assert st["restore_pending"]["restart_budget_spent"] is True
    from mcpbrain import doctor
    line = doctor.restore_pending_line(st)
    assert "mcpbrain is holding sync so your backup is not overwritten" in line
    assert "restart the daemon while online" in line


def test_startup_waits_out_a_short_outage_before_any_writer(home, monkeypatch):
    clock = Clock()
    rec = _restore_rig(home, monkeypatch, find_errors=[_transient(), _transient()])
    d = _daemon(home, clock)
    d._stop = AdvancingStop(clock)
    d._startup_restore()
    assert d._stop.waits == [60.0, 120.0]
    assert rec["restored"] == ["SNAP1"]
    assert d._restore_pending is False
    assert dmod._read_restore_pending(str(home)) is None


def test_startup_wait_is_capped_at_30_minutes(home, monkeypatch):
    clock = Clock()
    _restore_rig(home, monkeypatch, find_errors=[_transient()] * 50)
    d = _daemon(home, clock)
    d._stop = AdvancingStop(clock)
    d._startup_restore()
    assert sum(d._stop.waits) <= dmod.RESTORE_STARTUP_WAIT_S
    assert d._restore_pending is True


def test_definitive_no_snapshot_lifts_the_hold(home, monkeypatch):
    clock = Clock()
    d, _rec = _held_daemon(home, monkeypatch, clock, snapshot=None,
                           find_errors=[_transient()])
    clock.t += 60
    d._retry_google_builds()
    assert d._restore_pending is False
    assert dmod._read_restore_pending(str(home)) is None
    assert d.status()["restore_pending"] is None


def test_non_transient_restore_failure_does_not_hold(home, monkeypatch):
    rec = _restore_rig(home, monkeypatch, restore_error=ValueError("bad key"))
    d = _daemon(home, Clock())
    with pytest.raises(ValueError):
        d._startup_restore()
    assert d._restore_pending is False
    assert dmod._read_restore_pending(str(home)) is None
    st = d.status()
    assert st["restore_error"] == "bad key"
    from mcpbrain import doctor
    assert doctor.restore_error_line(st).startswith("❌")
    assert rec["restored"] == []


def test_n3_marker_unlink_failure_keeps_the_hold_and_retries(home, monkeypatch):
    clock = Clock()
    d, _rec = _held_daemon(home, monkeypatch, clock, snapshot=None,
                           find_errors=[_transient()])
    real_clear = dmod._clear_restore_pending
    results = [False]
    monkeypatch.setattr(dmod, "_clear_restore_pending",
                        lambda home: results.pop(0) if results else real_clear(home))
    clock.t += 60
    d._retry_google_builds()                 # no snapshot, but the unlink fails
    assert d._restore_pending is True
    d._retry_google_builds()                 # next tick retries the unlink
    assert d._restore_pending is False
    assert dmod._read_restore_pending(str(home)) is None


def test_n3_marker_write_failure_holds_in_memory(home, monkeypatch):
    monkeypatch.setattr(dmod, "_write_restore_pending", lambda home, reason: False)
    d, _rec = _held_daemon(home, monkeypatch, Clock(), find_errors=[_transient()])
    assert dmod._read_restore_pending(str(home)) is None
    assert d._restore_pending is True
    d._backup, d._backup_interval_s = object(), 3600.0
    assert d.maybe_backup() == {"backed_up": False, "skipped": "restore_pending"}


def test_n4_marker_with_backup_unconfigured_is_cleared(home, monkeypatch):
    dmod._write_restore_pending(str(home), "earlier outage")
    d = _daemon(home, Clock())                # no backup block in config.json
    d._startup_restore()
    assert d._restore_pending is False
    assert dmod._read_restore_pending(str(home)) is None


def test_doctor_counts_restore_pending_as_an_action():
    from mcpbrain import doctor
    from tests.test_doctor import _conns, _ok_repairs
    code, msg = doctor.run_doctor(
        "/tmp/home", model_present=lambda h: True, conns=_conns(),
        repairs=_ok_repairs(),
        daemon_status={"watchdog_exits": 0, "watchdog_limit_reached": False,
                       "restore_pending": {"since": "2026-10-09T08:00:00Z"}})
    assert "⚠️  Restore" in msg and "holding sync" in msg
    assert code == 1


def test_prune_held_is_surfaced_in_status(home, monkeypatch):
    _configure_backup(home)
    monkeypatch.setattr(auth, "build_google_services",
                        FlakyBuild(result={"drive_service": object()}))
    monkeypatch.setattr(dmod, "_mac_user_is_active", lambda: True)
    monkeypatch.setattr(dmod, "make_encrypted_snapshot", lambda *a, **k: home / "s.enc")
    monkeypatch.setattr(dmod, "upload_snapshot", lambda *a: "F")

    def prune(*a, held=None, **k):
        held.update(newest_bytes=10, largest_kept_bytes=1000)
        return 0
    monkeypatch.setattr("mcpbrain.backup.prune_snapshots", prune)
    d = _daemon(home, Clock())
    d.apply_config({})
    assert d.maybe_backup()["backed_up"] is True
    assert d.status()["prune_held"] == {"newest_bytes": 10, "largest_kept_bytes": 1000}


# -- the restoring gate + the busy-checked swap (real sqlite) -----------------

def test_store_gate_refuses_connections_while_restoring(tmp_path):
    from mcpbrain.store import StoreRestoringError
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    with s.restoring_gate():
        with pytest.raises(StoreRestoringError):
            s.chunk_count()
    assert s.chunk_count() == 0


def test_control_api_answers_503_while_restoring(tmp_path):
    import json
    import urllib.error
    import urllib.request
    from mcpbrain.control_api import ControlServer

    class D:
        restoring = True

        def is_restoring(self):
            return self.restoring

        def status(self):
            return {"ok": True}
    d = D()
    srv = ControlServer(d, home=str(tmp_path))
    srv.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/status")
        req.add_header("Authorization", f"Bearer {srv.token}")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 503
        d.restoring = False
        assert json.loads(urllib.request.urlopen(req).read()) == {"ok": True}
    finally:
        srv.stop()


def test_swap_refused_while_a_reader_holds_the_store_and_nothing_corrupts(tmp_path,
                                                                          monkeypatch):
    import shutil
    import sqlite3

    from mcpbrain import backup as bmod

    live = Store(tmp_path / "live.sqlite3", dim=4)
    live.init()
    # A reader in "another process" (its own connection), kept open so the WAL
    # survives, then holding a read transaction while more frames are written.
    reader = sqlite3.connect(str(live.path))
    live.upsert_chunk(doc_id="a", text="one", content_hash="h1", metadata={})
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM chunks").fetchone()
    live.upsert_chunk(doc_id="b", text="two", content_hash="h2", metadata={})

    snap = Store(tmp_path / "snap.sqlite3", dim=4)
    snap.init()
    for i in range(3):
        snap.upsert_chunk(doc_id=f"s{i}", text="snap", content_hash=f"s{i}", metadata={})
    sqlite3.connect(str(snap.path)).execute("PRAGMA wal_checkpoint(TRUNCATE)").close()

    monkeypatch.setattr(bmod, "download_snapshot", lambda svc, fid, dest: dest)
    monkeypatch.setattr(bmod, "restore",
                        lambda enc, stage, key: shutil.copyfile(snap.path, stage))

    class BC:
        drive_service, key = "DRIVE", b"k"

    with pytest.raises(bmod.RestoreBusyError):
        bmod.download_and_restore(BC(), live, "F")
    reader.rollback()
    reader.close()

    def integrity(path):
        con = sqlite3.connect(str(path))
        try:
            return con.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            con.close()
    assert integrity(live.path) == "ok"
    assert live.chunk_count() == 2            # untouched by the refused swap
    assert not list(tmp_path.glob(".restore-*"))

    # With the reader gone the same swap goes through cleanly.
    bmod.download_and_restore(BC(), live, "F")
    assert integrity(live.path) == "ok"
    assert live.chunk_count() == 3
    con = sqlite3.connect(str(live.path))
    try:
        ids = {r[0] for r in con.execute("SELECT doc_id FROM chunks")}
    finally:
        con.close()
    assert ids == {"s0", "s1", "s2"}


def test_busy_swap_is_classified_transient():
    from mcpbrain import backup as bmod
    assert dmod._is_transient_google_error(bmod.RestoreBusyError("busy"))


# -- M1: a concurrent apply_config wins --------------------------------------

def test_retry_rechecks_under_build_lock(home, monkeypatch):
    clock = Clock()
    build = FlakyBuild(result={"drive_service": object()})
    monkeypatch.setattr(auth, "build_google_services", build)
    _configure_backup(home)
    d = _daemon(home, clock)
    d._backup_backoff.failed(_transient())
    clock.t += 60
    bo = d._backup_backoff
    real_acquire = bo.build_lock.acquire

    class Lock:
        def acquire(self, blocking=True):
            ok = real_acquire(blocking)
            bo.clear()                     # apply_config succeeded meanwhile
            return ok

        def release(self):
            bo.build_lock_real.release()
    bo.build_lock_real = bo.build_lock
    bo.build_lock = Lock()
    d._retry_backup_build()
    assert build.calls == 0


# -- M5: wiring ----------------------------------------------------------------

def test_main_seeds_the_backoffs_from_transient_startup_failures(home, monkeypatch):
    import mcpbrain.embed as embed_module
    import mcpbrain.store as store_module

    class FakeStore:
        def __init__(self, *a, **k):
            pass

        def init(self):
            pass

    monkeypatch.setattr(embed_module, "get_embedder", lambda kind=None: FakeEmbedder())
    monkeypatch.setattr(store_module, "Store", FakeStore)
    org_exc, bk_exc = _transient(), _transient()
    monkeypatch.setattr(dmod, "_maybe_merge_org_config", lambda home: org_exc)
    monkeypatch.setattr(dmod, "_backup_from_config",
                        lambda home: dmod._BackupBuild(transient=bk_exc))
    seen = {}

    def fake_run_one(self):
        seen["d"] = self
        return {}
    monkeypatch.setattr(Daemon, "run_one", fake_run_one)
    monkeypatch.setattr(Daemon, "ensure_services", lambda self: {})
    dmod.main(["--once"])
    d = seen["d"]
    assert d._org_config_backoff.pending and d._org_config_backoff.attempts == 1
    assert d._backup_backoff.pending and d._backup_backoff.attempts == 1
    assert d._backup is None


def test_run_loop_iteration_calls_retry_google_builds(monkeypatch):
    import threading
    dm = Daemon.__new__(Daemon)
    dm._lock = threading.Lock()
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._wake = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._progress = {}
    dm._progress_lock = threading.Lock()
    dm._clock = lambda: 0.0
    dm._interval_s = 0.01
    dm._pending_update = False
    dm._maintenance_thread = None
    dm._maintenance_interval_s = 3600.0
    order = []
    monkeypatch.setattr(dm, "_start_maintenance_thread", lambda: None)
    monkeypatch.setattr(dm, "_startup_restore", lambda: None)
    monkeypatch.setattr(dm, "run_one", lambda: dm._stop.set() or {})
    monkeypatch.setattr(dm, "ensure_services", lambda: {})
    monkeypatch.setattr(dm, "_retry_google_builds", lambda: order.append("retry"))
    monkeypatch.setattr(dm, "_backup_under_bulk_lock", lambda: order.append("backup"))
    monkeypatch.setattr(dmod, "write_daemon_heartbeat", lambda home: None)
    dm.run()
    assert order == ["retry", "backup"]


# -- fix round 3 ----------------------------------------------------------------

def _snapshot_artifact(tmp_path, n=3):
    """A real, encrypted bare-store snapshot holding `n` chunks, plus its key."""
    import sqlite3

    from mcpbrain import backup as bmod
    src = Store(tmp_path / "snapsrc.sqlite3", dim=4)
    src.init()
    for i in range(n):
        src.upsert_chunk(doc_id=f"s{i}", text="snap", content_hash=f"s{i}", metadata={})
    sqlite3.connect(str(src.path)).execute("PRAGMA wal_checkpoint(TRUNCATE)").close()
    key = bmod.generate_escrow_key()
    art = tmp_path / "artifact.enc"
    bmod.encrypt_file(src.path, art, key)
    return art, key


def _integrity(path):
    import sqlite3
    con = sqlite3.connect(str(path))
    try:
        return con.execute("PRAGMA integrity_check").fetchone()[0]
    finally:
        con.close()


def test_held_daemon_bootstrap_baseline_is_skipped_api_and_doctor(home, monkeypatch):
    import json
    import urllib.request

    from mcpbrain import doctor, onboarding
    from mcpbrain.control_api import ControlServer
    d, _rec = _held_daemon(home, monkeypatch, Clock(), find_errors=[_transient()])

    def must_not_import(home_, store, **kw):
        store.upsert_chunk(doc_id="cached", text="x", content_hash="c", metadata={})
        return {"status": "done"}
    monkeypatch.setattr(onboarding, "run_bootstrap", must_not_import)
    monkeypatch.setattr(onboarding, "should_bootstrap", lambda home_: True)

    srv = ControlServer(d, home=str(home), store=d._store)
    srv.start()
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/bootstrap-baseline",
                                     data=b"{}", method="POST")
        req.add_header("Authorization", f"Bearer {srv.token}")
        req.add_header("Content-Type", "application/json")
        body = json.loads(urllib.request.urlopen(req).read())
    finally:
        srv.stop()
    assert body == {"status": "skipped", "reason": "restore pending"}
    assert d._store.chunk_count() == 0

    # The doctor repair path reports it as held, not as a failure.
    from tests.test_doctor import _conns, _ok_repairs
    repairs = {**_ok_repairs(), "baseline": lambda: d.bootstrap_baseline_once(force=True)}
    _code, msg = doctor.run_doctor("/tmp/home", model_present=lambda h: True,
                                   conns=_conns(), repairs=repairs, daemon_status={})
    assert "➖ Baseline" in msg and "restore pending" in msg
    assert d._store.chunk_count() == 0


def test_held_daemon_refuses_store_writing_tools_but_answers_read_only(home, monkeypatch):
    from mcpbrain.control_api import RestoreHoldRefused
    d, _rec = _held_daemon(home, monkeypatch, Clock(), find_errors=[_transient()])
    with pytest.raises(RestoreHoldRefused, match="restore"):
        d.call_tool("brain_draft_save", {"thread_id": "t", "body": "b"})
    with pytest.raises(RestoreHoldRefused):
        d.call_tool("brain_finding_resolve", {"finding_id": 1, "outcome": "fixed"})
    # A read-only tool still answers.
    d.call_tool("brain_actions", {"owner": "Sam", "status": "open"})
    assert d._store.chunk_count() == 0


def test_tool_refusal_is_a_409_error_over_the_control_api(home, monkeypatch):
    import json
    import urllib.error
    import urllib.request

    from mcpbrain.control_api import ControlServer
    d, _rec = _held_daemon(home, monkeypatch, Clock(), find_errors=[_transient()])
    srv = ControlServer(d, home=str(home), store=d._store)
    srv.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/api/tool", method="POST",
            data=json.dumps({"name": "brain_draft_save",
                             "arguments": {"thread_id": "t", "body": "b"}}).encode())
        req.add_header("Authorization", f"Bearer {srv.token}")
        req.add_header("Content-Type", "application/json")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 409
        assert "waiting to restore your backup" in json.loads(e.value.read())["error"]
    finally:
        srv.stop()


def test_permanent_probe_error_lifts_the_hold_and_reports(home, monkeypatch):
    clock = Clock()
    d, _rec = _held_daemon(home, monkeypatch, clock,
                           find_errors=[_transient(), PermissionError("forbidden")])
    clock.t += 60
    d._retry_google_builds()
    assert d._restore_pending is False
    assert dmod._read_restore_pending(str(home)) is None
    assert d.status()["restore_error"] == "forbidden"


def test_store_gate_waits_for_an_in_flight_connection(tmp_path):
    import threading

    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    inside, release, order = threading.Event(), threading.Event(), []

    def hold():
        with s._connect() as db:
            db.execute("SELECT 1").fetchone()
            inside.set()
            release.wait(5)
        order.append("closed")
    t = threading.Thread(target=hold)
    t.start()
    inside.wait(5)
    threading.Timer(0.1, release.set).start()
    with s.restoring_gate(timeout_s=5):
        order.append("gated")
    t.join(5)
    assert order == ["closed", "gated"]


def test_store_gate_times_out_and_reopens(tmp_path):
    import threading

    from mcpbrain.store import StoreRestoringError
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    inside, release = threading.Event(), threading.Event()

    def hold():
        with s._connect():
            inside.set()
            release.wait(5)
    t = threading.Thread(target=hold)
    t.start()
    inside.wait(5)
    try:
        with pytest.raises(StoreRestoringError):
            with s.restoring_gate(timeout_s=0.05):
                pass
        assert s.chunk_count() == 0          # the gate reopened after timing out
    finally:
        release.set()
        t.join(5)


def test_startup_restore_end_to_end_through_the_restoring_gate(home, monkeypatch):
    """The real download_and_restore through Daemon._restore_gate: a local fake
    Drive (download_snapshot copies a real encrypted snapshot), a real store."""
    import shutil

    from mcpbrain import backup as bmod
    art, key = _snapshot_artifact(home)
    from mcpbrain import config
    config.write_config(str(home), {"backup": {
        "escrow_key": key.decode(), "shared_drive_id": "SD", "user_id": "u1"}})
    monkeypatch.setattr(auth, "build_google_services",
                        FlakyBuild(result={"drive_service": "DRIVE"}))
    monkeypatch.setattr(bmod, "find_latest_snapshot", lambda svc, sd, uid: "SNAP1")
    monkeypatch.setattr(bmod, "download_snapshot",
                        lambda svc, fid, dest, **kw: shutil.copyfile(art, dest))
    d = _daemon(home, Clock())
    seen = []
    real_ckpt = bmod._checkpoint_for_swap

    def spy(path):
        seen.append((d.is_restoring(), d._store._gate_closed))
        return real_ckpt(path)
    monkeypatch.setattr(bmod, "_checkpoint_for_swap", spy)

    d._startup_restore()
    assert seen == [(True, True)]             # swapped only behind both gates
    assert not d.is_restoring()
    assert d._store.chunk_count() == 3
    assert _integrity(d._store.path) == "ok"
    assert not list(home.glob(".restore-*"))


def test_wizard_restore_is_busy_checked_and_never_in_place(home, monkeypatch):
    """/api/backup/auto -> run_restore_auto -> Daemon.swap_restored_store:
    refused (503, retry) while a reader holds the store, intact afterwards,
    then swapped in cleanly."""
    import json
    import shutil
    import sqlite3
    import urllib.error
    import urllib.request

    from mcpbrain import backup as bmod
    from mcpbrain import restore as rmod
    from mcpbrain.control_api import ControlServer
    art, key = _snapshot_artifact(home)
    monkeypatch.setattr(dmod.config, "store_path", lambda: home / "b.sqlite3")
    monkeypatch.setattr("mcpbrain.config.store_path", lambda: home / "b.sqlite3")
    monkeypatch.setattr(rmod, "account_email", lambda h: "sam@example.org")
    monkeypatch.setattr(rmod, "detect_restorable", lambda h, svc: {
        "available": True, "snapshot_id": "SNAP1", "user_email": "sam@example.org",
        "escrow_folder_id": "ESC", "has_key": True})
    monkeypatch.setattr(rmod, "_download_escrow_key", lambda svc, folder, email: key)
    monkeypatch.setattr(auth, "load_credentials", lambda *a, **k: object())
    monkeypatch.setattr(auth, "build_service", lambda *a, **k: object())
    monkeypatch.setattr(bmod, "download_snapshot",
                        lambda svc, fid, dest, **kw: shutil.copyfile(art, dest))

    d = _daemon(home, Clock())               # empty store at home/b.sqlite3
    # A reader in another connection holding a read transaction over live frames.
    reader = sqlite3.connect(str(d._store.path))
    reader.execute("CREATE TABLE IF NOT EXISTS t(x)")
    reader.commit()
    d._store.set_meta("k", "1")
    reader.execute("BEGIN")
    reader.execute("SELECT COUNT(*) FROM chunks").fetchone()
    d._store.set_meta("k", "2")

    srv = ControlServer(d, home=str(home), store=d._store)
    srv.start()

    def post():
        req = urllib.request.Request(f"http://127.0.0.1:{srv.port}/api/backup/auto",
                                     data=b"{}", method="POST")
        req.add_header("Authorization", f"Bearer {srv.token}")
        req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req)
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            post()
        assert e.value.code == 503
        assert json.loads(e.value.read())["retry"] is True
        reader.rollback()
        reader.close()
        assert _integrity(d._store.path) == "ok"
        assert d._store.chunk_count() == 0
        assert json.loads(post().read())["action"] == "restored"
    finally:
        srv.stop()
    assert d._store.chunk_count() == 3
    assert _integrity(d._store.path) == "ok"
    assert not list(home.glob(".restore-*"))


def _http_error(status):
    import httplib2
    from googleapiclient.errors import HttpError
    return HttpError(httplib2.Response({"status": status}), b"server error")


def test_probe_treats_a_drive_500_as_transient_and_keeps_the_hold(home, monkeypatch):
    clock = Clock()
    d, rec = _held_daemon(home, monkeypatch, clock,
                          find_errors=[_transient(), _http_error(500)])
    clock.t += 60
    d._retry_google_builds()
    assert d._restore_pending is True                       # NOT lifted by a 500
    assert dmod._read_restore_pending(str(home)) is not None
    assert d.status().get("restore_error") is None
    assert rec["restored"] == []


def test_probe_5xx_widening_does_not_touch_reflow_classifier():
    from mcpbrain.sync.reflow_handler import _is_transient
    assert _is_transient(_http_error(500)) is False         # reflow: a plain 500 stays permanent
    assert dmod._is_transient_probe_error(_http_error(500)) is True
    assert dmod._is_transient_probe_error(_http_error(599)) is True
    assert dmod._is_transient_probe_error(_http_error(404)) is False
    assert dmod._is_transient_probe_error(PermissionError("forbidden")) is False
    wrapped = RuntimeError("lookup failed")
    wrapped.__cause__ = _http_error(500)
    assert dmod._is_transient_probe_error(wrapped) is True
