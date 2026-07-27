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
