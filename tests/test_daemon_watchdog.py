"""Stall detection and platform-aware self-healing."""
import json
import threading

import pytest

from mcpbrain import daemon as d


def _wd_daemon(tmp_path, monkeypatch=None, now=1000.0):
    dm = d.Daemon.__new__(d.Daemon)
    dm._clock = lambda: now
    dm._progress = {}
    dm._progress_lock = threading.Lock()
    # Daemon has no _home attribute; it resolves the app dir on demand.
    if monkeypatch is not None:
        monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    return dm


def test_note_progress_records_a_timestamp(tmp_path):
    dm = _wd_daemon(tmp_path)
    dm._note_progress("sync")
    assert dm._progress["sync"] == 1000.0


def test_no_stall_when_progress_is_fresh(tmp_path):
    dm = _wd_daemon(tmp_path)
    dm._note_progress("sync")
    assert dm._stalled_phase() is None


def test_stall_detected_after_threshold(tmp_path):
    dm = _wd_daemon(tmp_path)
    dm._note_progress("sync")
    dm._clock = lambda: 1000.0 + d.STALL_S + 1.0
    stalled = dm._stalled_phase()
    assert stalled is not None
    assert stalled[0] == "sync"


def test_no_stall_before_any_progress_recorded(tmp_path):
    """A daemon that has not started work yet is not stalled."""
    dm = _wd_daemon(tmp_path)
    assert dm._stalled_phase() is None


def test_stall_detected_on_stalest_phase_not_freshest(tmp_path):
    """Regression: _maintenance_loop ticks "maintenance" every ~60s on its own
    thread, independent of the bulk sync/cycle thread. If _stalled_phase picked
    the freshest phase (as an earlier version did via max() instead of min()),
    a live "maintenance" heartbeat would mask a genuinely wedged "sync"/"cycle"
    forever -- exactly the failure mode this whole task exists to catch."""
    dm = _wd_daemon(tmp_path)
    dm._note_progress("sync")             # last real work: far in the past
    dm._clock = lambda: 1000.0 + d.STALL_S + 1.0
    dm._note_progress("maintenance")       # maintenance thread ticked just now
    stalled = dm._stalled_phase()
    assert stalled is not None
    assert stalled[0] == "sync"


def test_exit_limiter_stops_after_three_in_window(tmp_path, monkeypatch):
    # _recent_watchdog_exits compares persisted history against time.time(),
    # not self._clock() (see test_watchdog_history_survives_a_reboot) -- pin
    # the wall clock so history "close to 1000.0" reads as recent the same
    # way it did back when the comparison was against the fake _clock.
    monkeypatch.setattr(d.time, "time", lambda: 1000.0)
    dm = _wd_daemon(tmp_path, monkeypatch)
    (tmp_path / "watchdog_exits.json").write_text(
        json.dumps([900.0, 950.0, 990.0]))          # 3 recent exits
    assert dm._watchdog_may_exit() is False


def test_exit_limiter_allows_when_window_has_aged_out(tmp_path, monkeypatch):
    dm = _wd_daemon(tmp_path, monkeypatch)
    old = 1000.0 - d.WATCHDOG_WINDOW_S - 10.0
    (tmp_path / "watchdog_exits.json").write_text(
        json.dumps([old, old - 1, old - 2]))
    assert dm._watchdog_may_exit() is True


def test_exit_limiter_allows_when_no_history(tmp_path, monkeypatch):
    dm = _wd_daemon(tmp_path, monkeypatch)
    assert dm._watchdog_may_exit() is True


def test_recovery_exits_on_macos(tmp_path, monkeypatch):
    """launchd KeepAlive=True restarts us, so a plain exit is correct."""
    dm = _wd_daemon(tmp_path)
    monkeypatch.setattr(d.sys, "platform", "darwin")
    called = {}
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.setdefault("exit", True))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.setdefault("spawn", True))
    dm._recover_from_stall()
    assert called == {"exit": True}


def test_recovery_spawns_replacement_on_unsupervised_windows(tmp_path, monkeypatch):
    """Startup-folder fallback has no supervisor, so exiting alone would kill us."""
    dm = _wd_daemon(tmp_path)
    monkeypatch.setattr(d.sys, "platform", "win32")
    monkeypatch.setattr(d, "win_persistence_mechanism", lambda: "startup")
    called = {}
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.setdefault("exit", True))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.setdefault("spawn", True))
    dm._recover_from_stall()
    assert called.get("spawn") is True


def test_recovery_exits_on_supervised_windows(tmp_path, monkeypatch):
    """With a schtasks RestartOnFailure task, exit is supervised."""
    dm = _wd_daemon(tmp_path)
    monkeypatch.setattr(d.sys, "platform", "win32")
    monkeypatch.setattr(d, "win_persistence_mechanism", lambda: "schtasks")
    called = {}
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.setdefault("exit", True))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.setdefault("spawn", True))
    dm._recover_from_stall()
    assert called.get("exit") is True


def test_spawn_replacement_detaches_the_successor_on_windows(tmp_path, monkeypatch):
    """close_fds is not detachment: without DETACHED_PROCESS the successor keeps
    the dying parent's console, and without CREATE_NEW_PROCESS_GROUP a console
    teardown aimed at the parent reaches it too."""
    import subprocess
    dm = _wd_daemon(tmp_path)
    monkeypatch.setattr(d.sys, "platform", "win32")
    # The flags are Windows-only names; stand them in so this runs on POSIX CI.
    monkeypatch.setattr(subprocess, "DETACHED_PROCESS", 0x8, raising=False)
    monkeypatch.setattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200, raising=False)
    seen = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: seen.update(argv=argv, kw=kw))
    monkeypatch.setattr(d.os, "_exit", lambda code: None)
    dm._spawn_replacement()
    assert seen["kw"]["creationflags"] == 0x8 | 0x200
    assert seen["kw"]["close_fds"] is True


def test_spawn_replacement_passes_no_creationflags_on_posix(tmp_path, monkeypatch):
    """DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP do not exist in POSIX
    subprocess — touching them unconditionally would break macOS/Linux."""
    import subprocess
    dm = _wd_daemon(tmp_path)
    monkeypatch.setattr(d.sys, "platform", "darwin")
    seen = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda argv, **kw: seen.update(argv=argv, kw=kw))
    monkeypatch.setattr(d.os, "_exit", lambda code: None)
    dm._spawn_replacement()
    assert "creationflags" not in seen["kw"]


class _Emb:
    dim = 4

    def embed_passages(self, texts):
        return [[0.0] * 4 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 4


def _real_daemon(tmp_path, monkeypatch):
    from mcpbrain.store import Store
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    store = Store(tmp_path / "s.sqlite3", dim=4)
    store.init()
    return d.Daemon(store, _Emb(), services={},
                    lock=d.SingleWriterLock(tmp_path / "d.lock"),
                    clock=lambda: 1000.0)


def test_status_reports_the_watchdog_restart_limiter(tmp_path, monkeypatch):
    """Spec: limiter state is exposed on /api/status for doctor and the tray.
    Without it, "stops self-restarting and stays visibly stuck" is visible
    nowhere at all."""
    # History is now compared against time.time(), not the daemon's fake
    # _clock (see test_watchdog_history_survives_a_reboot) -- pin the wall
    # clock so the fixture's "close to 1000.0" entries still read as recent.
    monkeypatch.setattr(d.time, "time", lambda: 1000.0)
    dm = _real_daemon(tmp_path, monkeypatch)
    (tmp_path / "watchdog_exits.json").write_text(json.dumps([900.0, 950.0, 990.0]))
    st = dm.status()
    assert st["watchdog_exits"] == 3
    assert st["watchdog_limit_reached"] is True
    # ...and agrees with the limiter that actually decides.
    assert dm._watchdog_may_exit() is False


def test_status_watchdog_clean_by_default(tmp_path, monkeypatch):
    dm = _real_daemon(tmp_path, monkeypatch)
    st = dm.status()
    assert st["watchdog_exits"] == 0
    assert st["watchdog_limit_reached"] is False


def test_daemon_seeds_cycle_progress_at_construction(tmp_path, monkeypatch):
    dm = _real_daemon(tmp_path, monkeypatch)
    assert dm._progress["cycle"] == 1000.0


def test_status_surfaces_watchdog_exit_count(tmp_path, monkeypatch):
    """The limiter's state has to be visible somewhere a human looks. Reuses the
    same history file the limiter reads, so the two can never disagree."""
    monkeypatch.setattr(d.time, "time", lambda: 1000.0)
    dm = _wd_daemon(tmp_path, monkeypatch)
    (tmp_path / "watchdog_exits.json").write_text(json.dumps([900.0, 950.0]))
    assert len(dm._recent_watchdog_exits()) == 2
    assert dm._watchdog_may_exit() is True


def test_recent_exits_drops_entries_outside_the_window(tmp_path, monkeypatch):
    monkeypatch.setattr(d.time, "time", lambda: 1000.0)
    dm = _wd_daemon(tmp_path, monkeypatch)
    old = 1000.0 - d.WATCHDOG_WINDOW_S - 10.0
    (tmp_path / "watchdog_exits.json").write_text(json.dumps([old, 950.0]))
    assert dm._recent_watchdog_exits() == [950.0]


def test_progress_seeded_with_cycle_so_a_first_cycle_wedge_is_visible():
    """_stalled_phase returns None on an empty _progress, and only the
    maintenance thread writes anything if the cycle wedges before its first
    run_one() returns — so an unseeded _progress makes a never-completed first
    cycle look permanently healthy. That is the live incident's shape."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._clock = lambda: 1000.0
    dm._progress_lock = threading.Lock()
    dm._progress = {"cycle": dm._clock()}          # what __init__ now does
    dm._clock = lambda: 1000.0 + d.STALL_S + 1.0   # first cycle never returned
    stalled = dm._stalled_phase()
    assert stalled is not None and stalled[0] == "cycle"


def test_resume_restamps_progress_so_a_long_pause_is_not_a_stall(tmp_path):
    """_maintenance_loop skips its whole body (watchdog included) while paused,
    so the timestamps freeze. A pause longer than STALL_S must not read as a
    stall on the first tick after resume."""
    dm = d.Daemon.__new__(d.Daemon)
    now = [1000.0]
    dm._clock = lambda: now[0]
    dm._progress_lock = threading.Lock()
    dm._progress = {"cycle": 1000.0, "sync": 1000.0}
    dm._pause = threading.Event()
    dm.pause()
    now[0] = 1000.0 + d.STALL_S * 2       # a long, legitimate pause
    assert dm._stalled_phase() is not None, "precondition: looks stalled while paused"
    dm.resume()
    assert dm._stalled_phase() is None, "resume must restart the watchdog's clock"
    assert set(dm._progress) == {"cycle", "sync"}


def test_watchdog_does_not_exit_during_a_backup(tmp_path, monkeypatch):
    """os._exit bypasses finally; killing mid-snapshot orphans the temp dir.
    That is how ~24GB of mcpbrain-snap-* was left behind on 2026-07-27."""
    dm = _wd_daemon(tmp_path, monkeypatch)
    dm._backup_in_progress = threading.Event()
    dm._backup_in_progress.set()
    called = []
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.append("exit"))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.append("spawn"))
    dm._recover_from_stall()
    assert called == [], "watchdog must not exit while a backup is running"


def test_watchdog_history_survives_a_reboot(tmp_path, monkeypatch):
    """Persisted timestamps must be wall-clock. time.monotonic resets on reboot,
    which would make every historical entry look recent and disable self-healing
    permanently."""
    dm = _wd_daemon(tmp_path, monkeypatch)
    dm._record_watchdog_exit()
    import json
    written = json.loads((tmp_path / "watchdog_exits.json").read_text())
    import time as _t
    assert abs(written[0] - _t.time()) < 60, "expected wall-clock, got monotonic"


def test_backup_phase_reports_progress(tmp_path, monkeypatch):
    """A multi-minute backup must not read as a stall."""
    dm = _wd_daemon(tmp_path, monkeypatch)
    dm._note_progress("backup")
    assert "backup" in dm._progress


def test_recovery_deferred_while_cycle_is_repeatedly_failing(tmp_path, monkeypatch):
    """A deterministic exception looks identical to a hang from _stalled_phase's
    point of view -- "cycle" simply stops advancing either way -- but a restart
    cannot fix a bug that fails the same way every time. _recover_from_stall
    must defer once the consecutive-failure streak run() tracks passes 3,
    rather than burning the watchdog's 3-exit budget restart-looping on
    something a restart can't solve."""
    dm = _wd_daemon(tmp_path, monkeypatch)
    dm._cycle_error_streak = 4
    called = []
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.append("exit"))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.append("spawn"))
    dm._recover_from_stall()
    assert called == [], "watchdog must not restart-loop a repeatedly-failing (not hung) cycle"


def test_recovery_still_fires_below_the_failure_threshold(tmp_path, monkeypatch):
    """Sanity check on the other side of the >3 threshold: a genuine stall with
    few or no recent cycle failures must still recover normally."""
    dm = _wd_daemon(tmp_path, monkeypatch)
    dm._cycle_error_streak = 2
    monkeypatch.setattr(d.sys, "platform", "darwin")
    called = {}
    monkeypatch.setattr(dm, "_exit_for_restart", lambda: called.setdefault("exit", True))
    monkeypatch.setattr(dm, "_spawn_replacement", lambda: called.setdefault("spawn", True))
    dm._recover_from_stall()
    assert called == {"exit": True}


def test_maintenance_loop_does_not_swallow_assertion_error(monkeypatch):
    """The outer `except Exception` around a maintenance tick used to also catch
    AssertionError -- exactly what tests' _no_real_exit safety net raises when
    the watchdog reaches a real os._exit. Swallowing it here would silently
    demote that regression to a WARNING log line instead of a failing test."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._maintenance_interval_s = 3600.0

    def _boom():
        raise AssertionError("os._exit(1) called in a test")

    monkeypatch.setattr(dm, "_run_periodic_passes", _boom)

    with pytest.raises(AssertionError):
        dm._maintenance_loop()


def test_run_restarts_maintenance_thread_if_it_dies(monkeypatch):
    """The watchdog lives inside _maintenance_loop (see _start_maintenance_thread),
    so if that thread dies, the daemon must not silently keep looping cycles
    forever with nobody watching for a wedge ever again."""
    dm = d.Daemon.__new__(d.Daemon)
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
    dm._cycle_error_streak = 0

    dead = threading.Thread(target=lambda: None)
    dead.start()
    dead.join()
    dm._maintenance_thread = dead

    restarted = []
    monkeypatch.setattr(dm, "_start_maintenance_thread", lambda: restarted.append(1))

    calls = {"n": 0}

    def _run_one():
        calls["n"] += 1
        if calls["n"] >= 2:
            dm._stop.set()
        return {}

    monkeypatch.setattr(dm, "run_one", _run_one)
    monkeypatch.setattr(dm, "ensure_services", lambda: {})
    monkeypatch.setattr(dm, "_backup_under_bulk_lock", lambda: None)
    monkeypatch.setattr(d, "write_daemon_heartbeat", lambda home: None)

    dm.run()

    # Once at startup (run() always starts it) + once more when the loop
    # notices the thread died and restarts it.
    assert restarted == [1, 1]


def test_run_tracks_and_resets_consecutive_cycle_failures(monkeypatch):
    """run()'s except handler must increment _cycle_error_streak on every raise
    and reset it on the next clean return -- the streak is how
    _recover_from_stall tells a repeatedly-failing cycle apart from a genuine
    wedge (see its docstring)."""
    dm = d.Daemon.__new__(d.Daemon)
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
    dm._cycle_error_streak = 0

    calls = {"n": 0}
    streak_before_reset = {}

    def _run_one():
        calls["n"] += 1
        if calls["n"] < 5:
            raise RuntimeError("boom")
        streak_before_reset["value"] = dm._cycle_error_streak
        dm._stop.set()
        return {}

    monkeypatch.setattr(dm, "run_one", _run_one)
    monkeypatch.setattr(dm, "ensure_services", lambda: {})
    monkeypatch.setattr(dm, "_start_maintenance_thread", lambda: None)
    monkeypatch.setattr(dm, "_backup_under_bulk_lock", lambda: None)
    monkeypatch.setattr(d, "write_daemon_heartbeat", lambda home: None)

    dm.run()

    assert streak_before_reset["value"] == 4, "4 consecutive raises before the success"
    assert dm._cycle_error_streak == 0, "a later success must reset the streak"
    # The exception path re-stamps "cycle" itself (proof the loop thread is
    # alive, just failing) -- see test_transient_cycle_error_does_not_leave_a_
    # stale_progress_key for why a distinct "cycle_error" key must NOT exist.
    assert "cycle_error" not in dm._progress
    assert "cycle" in dm._progress


def test_transient_cycle_error_does_not_leave_a_stale_progress_key():
    """Critical regression, reproduced directly in review: _stalled_phase()
    takes the MINIMUM timestamp over EVERY key in _progress and nothing ever
    prunes a key. A distinct "cycle_error" key written only on failure and
    never touched again would go permanently stale after a single transient
    error (one Gmail timeout -- literally the motivating case for this whole
    step) even while the REST of the daemon keeps ticking normally --
    reproduced directly: all real phases (cycle/sync/maintenance/backup)
    stamped fresh every 60s for an hour, one cycle_error stamp from an hour
    ago -> _stalled_phase() returns ('cycle_error', 3600.0), tripping the
    watchdog on an otherwise perfectly healthy daemon. The fix re-stamps
    "cycle" itself on the exception path (see run()) instead of a side key,
    so there is no longer any key that can go stale this way on its own."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._progress_lock = threading.Lock()
    now = [1000.0]
    dm._clock = lambda: now[0]
    dm._progress = {}

    # t=1000: one transient error. run()'s except handler's behavior --
    # re-stamp "cycle", not a separate "cycle_error" key.
    dm._note_progress("cycle")

    # The daemon then runs perfectly for the next hour: every real phase
    # ticks fresh every 60s (cycle thread + independent maintenance thread +
    # a due backup cadence).
    for _ in range(60):
        now[0] += 60.0
        dm._note_progress("cycle")
        dm._note_progress("sync")
        dm._note_progress("maintenance")
        dm._note_progress("backup")

    assert "cycle_error" not in dm._progress
    assert dm._stalled_phase() is None, (
        "a single transient cycle error an hour ago must not still read as "
        "stalled once the rest of the daemon has kept ticking normally"
    )


def test_status_suppresses_stalled_while_paused(tmp_path, monkeypatch):
    """status() must not report stalled while paused: _maintenance_loop skips
    its whole body (progress heartbeat included) while paused, so every
    timestamp freezes and a long-enough pause would otherwise look exactly
    like a stall."""
    dm = _real_daemon(tmp_path, monkeypatch)
    dm._progress["cycle"] = dm._clock() - d.STALL_S - 1.0
    dm.pause()
    st = dm.status()
    assert st["stalled"] is None


def test_status_surfaces_backup_in_progress(tmp_path, monkeypatch):
    """status() was already in this task's scope; a backup-deferred-recovery
    state should be visible there too, not only as a log line -- otherwise
    "why isn't the watchdog restarting a wedged daemon" is undebuggable from
    the outside when the real answer is "a backup is in flight, deliberately".
    """
    dm = _real_daemon(tmp_path, monkeypatch)
    assert dm.status()["backup_in_progress"] is False
    dm._backup_in_progress.set()
    assert dm.status()["backup_in_progress"] is True


def test_backup_progress_stamped_even_when_the_acquire_times_out(tmp_path, monkeypatch):
    """Important regression, same false-stall shape as the cycle_error
    Critical via a different key: _note_progress("backup") used to run only
    AFTER a successful bulk-lock acquire, so on the acquire-timeout skip path
    "backup" was never touched. If a gated maintenance pass legitimately held
    _bulk_lock continuously past STALL_S, "backup" would age out on its own
    every cycle this method skipped -- a false stall on an otherwise idle,
    healthy daemon."""
    dm = _wd_daemon(tmp_path, monkeypatch)
    dm._bulk_lock = threading.Lock()
    dm._bulk_lock.acquire()          # simulate a gated pass already holding it
    dm._bulk_lock_wait_s = 0.01
    dm._bulk_lock_waiters = 0
    dm._bulk_lock_waiters_lock = threading.Lock()
    dm._backup_in_progress = threading.Event()
    monkeypatch.setattr(dm, "maybe_backup", lambda: None)

    dm._backup_under_bulk_lock()

    assert "backup" in dm._progress, "must stamp progress even when the acquire times out"


def test_start_maintenance_thread_creates_a_fresh_thread(monkeypatch):
    """Strengthens test_run_restarts_maintenance_thread_if_it_dies (which only
    proves run() CALLS _start_maintenance_thread() again on a dead thread) by
    confirming the method itself actually creates and starts a NEW Thread
    object running _maintenance_loop -- not e.g. a no-op or a redundant
    .start() on the already-dead one."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    ran = threading.Event()
    monkeypatch.setattr(dm, "_maintenance_loop", lambda: ran.set())

    old = threading.Thread(target=lambda: None)
    old.start()
    old.join()
    dm._maintenance_thread = old

    dm._start_maintenance_thread()

    assert dm._maintenance_thread is not old
    assert isinstance(dm._maintenance_thread, threading.Thread)
    dm._maintenance_thread.join(timeout=2)
    assert ran.is_set(), "the new thread must actually run _maintenance_loop"
