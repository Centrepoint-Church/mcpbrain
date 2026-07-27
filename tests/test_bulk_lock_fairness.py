"""A busy cycle must not starve the four gated passes indefinitely.

Live evidence from the reviewed build: 183 consecutive
"bulk lock held for more than 5.0s" warnings and not one gated pass run. The
cycle held _bulk_lock for all of run_one() and re-acquired 1s later, so with
non-FIFO locks the maintenance thread lost essentially every race.

test_cycle_yields_when_maintenance_wants_the_lock and
test_bulk_section_releases_between_phases exercise the fairness HAND-OFF
mechanism (_bulk_lock_intent / BULK_LOCK_YIELD_S) in isolation and are kept as
mechanism documentation, but a soak test run against the REAL section
granularity the daemon originally used (one section per whole run_sync_cycle/
drain.drain call, each budget-bounded to CYCLE_BUDGET_S=60s) found the
hand-off makes NO measurable difference at that granularity: 1/32 gated-pass
runs at a 60s lock hold, 0/32 at 300s -- this exactly reproduces the live
8m39s/183-skip failure. What actually restored throughput was splitting the
SAME work into many smaller sections (20x3s sections -> 36 runs, 0 skips),
independent of the hand-off. So this is a lock DUTY-CYCLE problem (held too
long per acquisition), not only a fairness problem -- the real fix is the
finer-grained sectioning now threaded through run_sync_cycle/index_pending/
drain/sync_gmail/sync_calendar/sync_drive/prepare_units (one section per
message/event/file/embed-batch/inbox-file, not per whole call).
test_gated_pass_runs_within_bounded_ticks_under_sustained_backlog below is the
soak-style regression test for THAT: it drives the real _cycle_bulk_section
and _run_periodic_passes at a small-section granularity under a cycle that
never idles, and asserts the gated pass actually gets a turn.
"""
import threading
import time

from mcpbrain import daemon as d
from mcpbrain.daemon import Daemon


def _dm():
    dm = d.Daemon.__new__(d.Daemon)
    dm._bulk_lock = threading.Lock()
    dm._bulk_lock_waiters = 0
    dm._bulk_lock_waiters_lock = threading.Lock()
    dm._bulk_lock_wait_s = 5.0
    dm._stop = threading.Event()
    return dm


def test_cycle_yields_when_maintenance_wants_the_lock():
    dm = _dm()
    got = []

    def maintenance():
        with dm._bulk_lock_intent():
            acquired = dm._bulk_lock.acquire(timeout=dm._bulk_lock_wait_s)
        got.append(acquired)
        if acquired:
            dm._bulk_lock.release()

    # Cycle does 20 short "phases", entering the bulk section each time.
    def cycle():
        for _ in range(20):
            with dm._cycle_bulk_section():
                time.sleep(0.02)

    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    time.sleep(0.05)
    m = threading.Thread(target=maintenance, daemon=True)
    m.start()
    m.join(timeout=10)
    c.join(timeout=10)

    assert got == [True], "maintenance never got the bulk lock while the cycle ran"


def test_bulk_section_releases_between_phases():
    """The lock must not be held across the whole cycle."""
    dm = _dm()
    seen_free = []

    def watcher():
        for _ in range(50):
            if dm._bulk_lock.acquire(blocking=False):
                dm._bulk_lock.release()
                seen_free.append(1)
                return
            time.sleep(0.01)

    def cycle():
        for _ in range(10):
            with dm._cycle_bulk_section():
                time.sleep(0.01)

    w = threading.Thread(target=watcher, daemon=True)
    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    w.start()
    w.join(timeout=5)
    c.join(timeout=5)
    assert seen_free, "bulk lock was never observably free during the cycle"


def test_two_independent_waiters_dont_erase_each_others_intent():
    """Regression for a single-Event _bulk_lock_wanted bug caught in review.

    _backup_under_bulk_lock runs on the CYCLE thread itself (called from
    run()'s loop, right after run_one()) and ALSO marks intent around its own
    bounded acquire -- so TWO independent call sites can be "waiting" at once
    (the maintenance thread's _run_periodic_passes, and the cycle thread's own
    backup call). With a single threading.Event, one waiter's cleanup
    (.clear() in its own finally) could erase the OTHER waiter's still-pending
    signal: maintenance sets the flag and blocks; the cycle thread's own
    backup call sets it too (already set, no-op), wins its own race, runs,
    and clears the flag in ITS finally -- even though maintenance is STILL
    genuinely waiting. The next _cycle_bulk_section release then sees no
    intent and skips the yield pause, right when it's needed. The counter-
    based _bulk_lock_intent/_bulk_lock_wanted design fixes this: cleanup only
    decrements, so the signal stays true as long as ANY waiter is pending.
    """
    dm = _dm()
    dm._bulk_lock.acquire()  # simulate a chunk-mutating phase in progress

    a_marked = threading.Event()
    a_done = threading.Event()

    def waiter_a():
        with dm._bulk_lock_intent():
            a_marked.set()
            dm._bulk_lock.acquire(timeout=2.0)  # never actually gets it in this test
        a_done.set()

    t = threading.Thread(target=waiter_a, daemon=True)
    t.start()
    assert a_marked.wait(timeout=2.0)

    # Waiter B marks intent and immediately clears it (simulating
    # _backup_under_bulk_lock's own short-lived intent window), while A is
    # STILL outstanding.
    with dm._bulk_lock_intent():
        assert dm._bulk_lock_wanted() is True, "B's own marking should also read True"
    # B's cleanup must NOT have erased A's still-pending signal.
    assert dm._bulk_lock_wanted() is True, (
        "a waiter's cleanup erased another waiter's still-pending intent "
        "(the single-Event bug this counter design fixes)"
    )

    dm._bulk_lock.release()
    assert a_done.wait(timeout=3.0)
    t.join(timeout=1.0)
    assert dm._bulk_lock_wanted() is False, "counter should be back to zero once both waiters finish"


def test_gated_pass_runs_within_bounded_ticks_under_sustained_backlog(monkeypatch):
    """Soak-style regression test at the granularity the daemon ACTUALLY uses.

    Drives the real _cycle_bulk_section and the real _run_periodic_passes
    (over one fake always-due gated pass) with a cycle thread that NEVER
    idles -- back-to-back small sections (~20ms each), simulating a
    sustained "more_work" backlog where the cycle keeps finding fresh work
    every tick, same as run_sync_cycle/drain.drain do when threaded a
    bulk_section per message/event/file/batch instead of per whole call.

    The 2s deadline is deliberately TIGHT, not generous: at this
    (~20ms-section) granularity the gated pass gets through almost
    immediately (measured: ~90ms, first tick) every time, so 2s is a huge
    margin for THIS design. But it is tight enough to genuinely discriminate
    against a regression to coarse sectioning -- verified directly: replacing
    the 20ms per-section sleep with a single hold of just 5s (still far
    smaller than a real ~60s CYCLE_BUDGET_S section) makes the gated pass
    time out on every attempt and never run within this same 2s window,
    because one coarse hold already outlasts the whole test. This is the
    test that should have caught the original 8m39s/183-skip failure (which
    was really "one section per ~minutes of work") and must keep catching a
    regression back to that coarse a granularity.
    """
    dm = d.Daemon.__new__(d.Daemon)
    dm._bulk_lock = threading.Lock()
    dm._bulk_lock_waiters = 0
    dm._bulk_lock_waiters_lock = threading.Lock()
    dm._bulk_lock_wait_s = 0.3
    dm._stop = threading.Event()
    dm._clock = time.monotonic

    gated_calls: list = []
    fake_pass = d.CadencePass(
        "fake_gated", "_fake_gated_interval_s", "_fake_gated_last",
        "_run_fake_gated", needs_configured=False, needs_bulk_lock=True)
    monkeypatch.setattr(d, "_CADENCE_PASSES", (fake_pass,))
    dm._run_fake_gated = lambda: gated_calls.append(1)
    dm._fake_gated_interval_s = 0.0   # always due
    dm._fake_gated_last = None

    stop_cycle = threading.Event()

    def cycle():
        # Never idles: one small section right after another, mirroring a
        # continuous backlog where run_sync_cycle/drain.drain always find
        # more work (each of THEIR own per-item bulk_section calls is this
        # short, not the whole ~60s call).
        while not stop_cycle.is_set():
            with dm._cycle_bulk_section():
                time.sleep(0.02)

    c = threading.Thread(target=cycle, daemon=True)
    c.start()
    try:
        deadline = time.monotonic() + 2.0
        ticks = 0
        while not gated_calls and time.monotonic() < deadline:
            dm._run_periodic_passes()
            ticks += 1
            time.sleep(0.05)
        assert gated_calls, (
            f"gated pass never ran within {ticks} maintenance ticks under a "
            "sustained (never-idle) backlog at small-section granularity"
        )
    finally:
        stop_cycle.set()
        c.join(timeout=2)


def test_run_one_actually_holds_bulk_lock_during_the_cycle(tmp_path, monkeypatch):
    """Regression for a silent-revert class Task 1 exists to prevent.

    `bulk_section` defaults to `contextlib.nullcontext` throughout this
    module's call chain (run_cycle -> run_sync_cycle -> sync_gmail, etc.), so
    deleting `bulk_section=self._cycle_bulk_section` from run_one's call to
    run_cycle would make the daemon run its whole cycle with NO bulk lock at
    all -- and the rest of the suite would very likely stay green, since
    almost no other test checks locking (they check the RESULT of a cycle,
    not whether _bulk_lock was held while producing it).

    This drives the REAL Daemon.run_one() -> run_cycle() -> run_sync_cycle()
    -> sync_gmail() path against real fixtures (not a direct call to
    _cycle_bulk_section in isolation) and observes, via a thin spy on
    store.upsert_chunk (the actual chunk-mutating call inside sync_gmail's
    bulk_section), that the daemon's own _bulk_lock is genuinely held at the
    moment a chunk is written.
    """
    from mcpbrain.daemon import SingleWriterLock
    from tests.test_daemon import FakeEmbedder, _gmail_fake_one_message, _make_store

    # Not autouse across modules -- test_daemon.py's own _isolate_app_home
    # fixture doesn't apply here, so this must set MCPBRAIN_HOME itself
    # (same reason that fixture exists: an unconfigured real backup would
    # otherwise get pulled in by run_one()'s startup path).
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = _make_store(tmp_path)
    emb = FakeEmbedder()
    fake = _gmail_fake_one_message()
    daemon = Daemon(store, emb, services={"gmail_service": fake},
                    lock=SingleWriterLock(tmp_path / "d.lock"))

    seen_locked: list = []
    orig_upsert = store.upsert_chunk

    def spy_upsert(*a, **kw):
        seen_locked.append(daemon._bulk_lock.locked())
        return orig_upsert(*a, **kw)

    store.upsert_chunk = spy_upsert

    res = daemon.run_one()

    assert res is not None
    assert seen_locked, "fixture produced no chunk writes to observe -- test setup is broken"
    assert all(seen_locked), (
        "run_one()'s real path wrote a chunk WITHOUT holding _bulk_lock -- the "
        "bulk_section=self._cycle_bulk_section wiring from run_one into "
        "run_cycle (and down into run_sync_cycle/sync_gmail) is missing or "
        "broken. bulk_section silently defaults to a no-op nullcontext, so "
        "this class of regression produces no other test failure."
    )
