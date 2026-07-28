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


def _dispatch_daemon(monkeypatch, tmp_path, *, wait_s=0.15, due=True):
    """A bare Daemon wired for a real _run_periodic_passes over two fake passes:
    one non-gated, one needing _bulk_lock. Returns (dm, ungated_calls,
    gated_calls)."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    dm = d.Daemon.__new__(d.Daemon)
    dm._bulk_lock = threading.Lock()
    dm._bulk_lock_waiters = 0
    dm._bulk_lock_waiters_lock = threading.Lock()
    dm._bulk_lock_wait_s = wait_s
    dm._clock = time.monotonic

    ungated_calls: list = []
    gated_calls: list = []

    fake_passes = (
        d.CadencePass("fake_ungated", "_fake_ungated_interval_s", "_fake_ungated_last",
                      "_run_fake_ungated", needs_configured=False, needs_bulk_lock=False),
        d.CadencePass("fake_gated", "_fake_gated_interval_s", "_fake_gated_last",
                      "_run_fake_gated", needs_configured=False, needs_bulk_lock=True),
    )
    monkeypatch.setattr(d, "_CADENCE_PASSES", fake_passes)
    dm._run_fake_ungated = lambda: ungated_calls.append(1)
    dm._run_fake_gated = lambda: gated_calls.append(1)
    # The gated entry is cadence-pre-checked before the lock is attempted:
    # interval None == pass OFF (never due), 0 == always due.
    dm._fake_ungated_interval_s = 0.0
    dm._fake_ungated_last = None
    dm._fake_gated_interval_s = 0.0 if due else None
    dm._fake_gated_last = None
    return dm, ungated_calls, gated_calls


def _hold_bulk_lock(dm, release_evt, hold_timeout=5.0):
    """Stand in for run()'s `with self._bulk_lock: self.run_one()` wedged
    mid-cycle: a real thread genuinely holding the real lock."""
    acquired = threading.Event()

    def _hold():
        with dm._bulk_lock:
            acquired.set()
            release_evt.wait(timeout=hold_timeout)

    holder = threading.Thread(target=_hold, daemon=True)
    holder.start()
    assert acquired.wait(timeout=2.0), "lock holder never acquired _bulk_lock"
    return holder


def test_bulk_lock_gates_only_the_four_chunk_writing_passes(monkeypatch, tmp_path):
    """Real _bulk_lock contention through the real dispatch loop.

    daemon.run() holds _bulk_lock for the whole run_one() cycle. A genuinely
    wedged cycle must not starve the ~16 non-gated cadence passes (that's Task
    4's fix); the four passes that also write `chunks` legitimately wait for the
    lock — but only for a BOUNDED wait, after which they are skipped for this
    tick and retried later.
    """
    dm, ungated_calls, gated_calls = _dispatch_daemon(monkeypatch, tmp_path, wait_s=5.0)
    release_lock = threading.Event()
    holder = _hold_bulk_lock(dm, release_lock)

    # With the lock held, the gated entry waits on it, so the dispatch loop must
    # not be run inline on the test's main thread.
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

    # Once the cycle releases the lock (well inside the 5s wait), it catches up.
    passes_thread.join(timeout=3.0)
    assert not passes_thread.is_alive()
    assert gated_calls == [1], "gated pass never ran once the bulk lock was released"


def test_dispatch_skips_a_lock_gated_pass_rather_than_blocking_forever(monkeypatch, tmp_path):
    """THE critical regression: the watchdog must stay reachable under contention.

    run() holds _bulk_lock for the whole of run_one(), so a wedged cycle holds it
    indefinitely. The dispatch loop used to do a plain `with self._bulk_lock:`,
    which parked the maintenance thread inside _run_periodic_passes for as long
    as the cycle was wedged -- taking _note_progress AND the _stalled_phase
    watchdog check (which run AFTER it in _maintenance_loop) down with it, and
    starving every pass ordered after the first gated one. The self-healing
    mechanism was therefore unreachable during exactly the stall it exists to
    detect. The acquire is now bounded: the pass is skipped for this tick.
    """
    dm, ungated_calls, gated_calls = _dispatch_daemon(monkeypatch, tmp_path, wait_s=0.15)
    release_lock = threading.Event()
    holder = _hold_bulk_lock(dm, release_lock)
    try:
        started = time.monotonic()
        dm._run_periodic_passes()      # must RETURN, inline, lock still held
        elapsed = time.monotonic() - started
    finally:
        release_lock.set()
        holder.join(timeout=2.0)

    assert elapsed < 2.0, f"dispatch blocked {elapsed:.1f}s on a held bulk lock"
    assert ungated_calls == [1], "non-gated pass did not run"
    assert gated_calls == [], "gated pass ran while the lock was held"


def test_maintenance_tick_completes_while_the_cycle_holds_the_bulk_lock(monkeypatch, tmp_path):
    """End-to-end shape of the same bug, through the real _maintenance_loop:
    with _bulk_lock held by a wedged cycle, the tick must still reach
    _note_progress and the watchdog check."""
    dm, ungated_calls, gated_calls = _dispatch_daemon(monkeypatch, tmp_path, wait_s=0.1)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._maintenance_interval_s = 0.01
    dm._progress = {}
    dm._progress_lock = threading.Lock()
    watchdog_checks: list = []
    dm._stalled_phase = lambda: watchdog_checks.append(1) and None

    release_lock = threading.Event()
    holder = _hold_bulk_lock(dm, release_lock, hold_timeout=10.0)
    t = threading.Thread(target=dm._maintenance_loop, daemon=True)
    t.start()
    try:
        deadline = time.monotonic() + 3.0
        while len(watchdog_checks) < 2 and time.monotonic() < deadline:
            time.sleep(0.01)
    finally:
        dm._stop.set()
        t.join(timeout=3.0)
        release_lock.set()
        holder.join(timeout=2.0)

    assert len(watchdog_checks) >= 2, (
        "maintenance tick never reached the watchdog check while _bulk_lock was held")
    assert "maintenance" in dm._progress, "progress heartbeat never advanced"
    assert len(ungated_calls) >= 2, "non-gated passes were starved"
    assert gated_calls == [], "gated pass ran while the lock was held"


def test_not_due_gated_pass_never_contends_for_the_bulk_lock(monkeypatch, tmp_path):
    """Cheap pre-gate: a gated pass that isn't due must not even attempt the
    lock, so a wedged cycle costs it nothing (not even the acquire timeout)."""
    dm, ungated_calls, gated_calls = _dispatch_daemon(
        monkeypatch, tmp_path, wait_s=5.0, due=False)
    release_lock = threading.Event()
    holder = _hold_bulk_lock(dm, release_lock)
    try:
        started = time.monotonic()
        dm._run_periodic_passes()
        elapsed = time.monotonic() - started
    finally:
        release_lock.set()
        holder.join(timeout=2.0)

    assert elapsed < 1.0, (
        f"a not-due gated pass waited {elapsed:.1f}s on the bulk lock; the "
        "cadence pre-check must run before the acquire")
    assert ungated_calls == [1]
    assert gated_calls == []


def test_gated_passes_self_gate_on_the_attrs_the_dispatch_pre_check_uses():
    """The dispatch loop checks a gated pass's cadence BEFORE taking _bulk_lock,
    using the descriptor's interval/last attrs. If a pass's own _is_due call ever
    drifted to different attrs, the outer pre-check would silently suppress it.
    (Only the gated passes are pre-checked — auto_update/verify resolve a default
    interval internally, so an outer check would wrongly disable them.)"""
    import inspect
    for cp in d._CADENCE_PASSES:
        if not cp.needs_bulk_lock:
            continue
        src = inspect.getsource(getattr(d.Daemon, cp.fn_name))
        assert f'self._is_due("{cp.interval_attr}", "{cp.last_attr}")' in src, (
            f"{cp.name} does not self-gate on the attrs its descriptor names")


def test_passes_run_while_the_cycle_thread_is_blocked():
    """The bug: maintenance was starved behind an unbounded run_one()."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._progress = {}
    dm._progress_lock = threading.Lock()
    dm._maintenance_interval_s = 0.01
    ran = []

    def _fake_passes():
        ran.append(1)

    dm._run_periodic_passes = _fake_passes
    dm._note_progress = lambda phase: None

    # Simulate the cycle loop wedged inside run_one(): a real thread genuinely
    # holding _bulk_lock (the same lock run()'s `with self._bulk_lock:` holds
    # for the whole cycle). The maintenance loop must keep ticking regardless,
    # since _maintenance_loop itself never touches _bulk_lock.
    holder_go = threading.Event()
    holder_release = threading.Event()

    def _hold_bulk_lock():
        with dm._bulk_lock:
            holder_go.set()
            holder_release.wait(timeout=5.0)

    holder = threading.Thread(target=_hold_bulk_lock, daemon=True)
    holder.start()
    assert holder_go.wait(timeout=2.0), "holder never acquired the lock"

    t = threading.Thread(target=dm._maintenance_loop, daemon=True)
    t.start()
    deadline = time.monotonic() + 2.0
    while len(ran) < 3 and time.monotonic() < deadline:
        time.sleep(0.01)
    dm._stop.set()
    t.join(timeout=2.0)
    holder_release.set()
    holder.join(timeout=5.0)

    assert len(ran) >= 3, f"scheduler only ticked {len(ran)} times"


def test_maintenance_loop_exits_on_stop():
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._pause = threading.Event()
    dm._bulk_lock = threading.Lock()
    dm._progress = {}
    dm._progress_lock = threading.Lock()
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
    dm._progress = {}
    dm._progress_lock = threading.Lock()
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
