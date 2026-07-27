"""Maintenance runs on its own thread, independent of the bulk cycle.

The regression this locks in: passes must fire even while run_one() is blocked.
Nothing in the suite covered that, which is why a four-day starvation went
unnoticed.
"""
import threading
import time

from mcpbrain import daemon as d


def test_four_chunk_writers_need_the_bulk_lock():
    need = {cp.name for cp in d._CADENCE_PASSES if cp.needs_bulk_lock}
    assert need == {"stale_reextract", "salience_score", "decay_pass", "consolidation"}


def test_passes_run_while_the_cycle_thread_is_blocked():
    """The bug: maintenance was starved behind an unbounded run_one()."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._maintenance_interval_s = 0.01
    ran = []

    def _fake_passes():
        ran.append(1)

    dm._run_periodic_passes = _fake_passes
    dm._note_progress = lambda phase: None

    # Simulate the cycle loop wedged inside run_one(): it holds nothing the
    # scheduler needs, so maintenance must keep ticking.
    wedged = threading.Event()
    threading.Thread(target=wedged.wait, daemon=True).start()

    t = threading.Thread(target=dm._maintenance_loop, daemon=True)
    t.start()
    deadline = time.monotonic() + 2.0
    while len(ran) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    dm._stop.set()
    t.join(timeout=2.0)
    wedged.set()

    assert len(ran) >= 3, f"scheduler only ticked {len(ran)} times"


def test_maintenance_loop_exits_on_stop():
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._maintenance_interval_s = 0.01
    dm._run_periodic_passes = lambda: None
    dm._note_progress = lambda phase: None

    t = threading.Thread(target=dm._maintenance_loop, daemon=True)
    t.start()
    dm._stop.set()
    t.join(timeout=2.0)
    assert not t.is_alive()


def test_maintenance_loop_survives_a_raising_pass():
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._maintenance_interval_s = 0.01
    calls = []

    def _boom():
        calls.append(1)
        raise RuntimeError("pass exploded")

    dm._run_periodic_passes = _boom
    dm._note_progress = lambda phase: None

    t = threading.Thread(target=dm._maintenance_loop, daemon=True)
    t.start()
    deadline = time.monotonic() + 2.0
    while len(calls) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    dm._stop.set()
    t.join(timeout=2.0)

    assert len(calls) >= 3, "a raising pass must not kill the scheduler"
