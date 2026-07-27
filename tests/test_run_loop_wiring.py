"""run() must start the maintenance thread and must NOT run passes inline.

The reviewed implementation could be reverted to the pre-fix shape — inline
_run_periodic_passes(), no thread — with every existing test still passing. That
is the exact bug this whole change exists to fix, so it needs a test that fails.
"""
import threading

from mcpbrain import daemon as d


def test_run_starts_the_maintenance_thread_and_does_not_run_passes_inline(monkeypatch, tmp_path):
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
    dm._maintenance_interval_s = 3600.0

    started = []
    inline = []
    monkeypatch.setattr(dm, "_start_maintenance_thread",
                        lambda: started.append(1))
    monkeypatch.setattr(dm, "_run_periodic_passes",
                        lambda: inline.append(1))
    monkeypatch.setattr(dm, "run_one", lambda: dm._stop.set() or {})
    monkeypatch.setattr(dm, "ensure_services", lambda: {})
    monkeypatch.setattr(dm, "_backup_under_bulk_lock", lambda: None)
    monkeypatch.setattr(d, "write_daemon_heartbeat", lambda home: None)

    dm.run()

    assert started == [1], "run() must start the maintenance thread"
    assert inline == [], "run() must NOT call _run_periodic_passes inline"
