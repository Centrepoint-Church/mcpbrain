"""Maintenance runs on its own thread, independent of the bulk cycle.

The regression this locks in: passes must fire even while run_one() is blocked.
Nothing in the suite covered that, which is why a four-day starvation went
unnoticed. test_bulk_lock_gates_only_the_four_chunk_writing_passes exercises
the real _bulk_lock contention path added to gate the four chunk-writing
passes against a genuinely wedged cycle.
"""
import threading
import time

from mcpbrain import daemon as d


def test_bulk_lock_gates_only_the_four_chunk_writing_passes(monkeypatch, tmp_path):
    """Real _bulk_lock contention through the real dispatch loop.

    daemon.run() now holds _bulk_lock for the whole run_one() cycle. A
    genuinely wedged cycle must not starve the ~16 non-gated cadence passes
    (that's this task's fix) but the four passes that also write `chunks`
    legitimately wait for the lock (the documented contention trade-off, not a
    bug) and catch up as soon as it's released.
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    dm = d.Daemon.__new__(d.Daemon)
    dm._bulk_lock = threading.Lock()

    ungated_calls = []
    gated_calls = []

    fake_passes = (
        d.CadencePass("fake_ungated", "_fake_ungated_interval_s", "_fake_ungated_last",
                      "_run_fake_ungated", needs_configured=False, needs_bulk_lock=False),
        d.CadencePass("fake_gated", "_fake_gated_interval_s", "_fake_gated_last",
                      "_run_fake_gated", needs_configured=False, needs_bulk_lock=True),
    )
    monkeypatch.setattr(d, "_CADENCE_PASSES", fake_passes)
    dm._run_fake_ungated = lambda: ungated_calls.append(1)
    dm._run_fake_gated = lambda: gated_calls.append(1)

    lock_acquired = threading.Event()
    release_lock = threading.Event()

    def _hold_lock():
        with dm._bulk_lock:
            lock_acquired.set()
            release_lock.wait(timeout=2.0)

    # Stand in for run()'s `with self._bulk_lock: cycle_result = self.run_one()`
    # wedged mid-cycle: a real thread genuinely holding the real lock.
    holder = threading.Thread(target=_hold_lock, daemon=True)
    holder.start()
    assert lock_acquired.wait(timeout=2.0), "lock holder never acquired _bulk_lock"

    # Run the real dispatch loop (Daemon._run_periodic_passes, unmocked) on its
    # own thread: with the lock held, the gated entry blocks acquiring it, so
    # this call must not be made inline on the test's main thread.
    passes_thread = threading.Thread(target=dm._run_periodic_passes, daemon=True)
    passes_thread.start()

    deadline = time.monotonic() + 2.0
    while not ungated_calls and time.monotonic() < deadline:
        time.sleep(0.01)
    assert ungated_calls == [1], "non-gated pass was blocked by a held bulk lock"
    # Give the gated entry a moment to (wrongly) get through if the dispatch
    # loop weren't actually gating it on the held lock.
    time.sleep(0.1)
    assert gated_calls == [], "gated pass ran despite the bulk lock being held"

    release_lock.set()
    holder.join(timeout=2.0)
    assert not holder.is_alive()

    # Once the cycle releases the lock, the gated pass catches up.
    passes_thread.join(timeout=2.0)
    assert not passes_thread.is_alive()
    assert gated_calls == [1], "gated pass never ran once the bulk lock was released"


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
