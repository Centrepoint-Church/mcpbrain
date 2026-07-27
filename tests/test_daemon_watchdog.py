"""Stall detection and platform-aware self-healing."""
import json
import threading

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
    dm = _wd_daemon(tmp_path, monkeypatch)
    (tmp_path / "watchdog_exits.json").write_text(json.dumps([900.0, 950.0]))
    assert len(dm._recent_watchdog_exits()) == 2
    assert dm._watchdog_may_exit() is True


def test_recent_exits_drops_entries_outside_the_window(tmp_path, monkeypatch):
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
