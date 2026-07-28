"""State shared between the cycle loop and the maintenance thread.

These are real-thread tests on purpose: the whole bug class lives in the
interleaving, and a mocked lock proves nothing.
"""
import threading
import time

from mcpbrain import daemon as d
from mcpbrain.store import Store


class _FakeEmb:
    dim = 4

    def embed_passages(self, texts):
        return [[0.0] * 4 for _ in texts]

    def embed_query(self, text):
        return [0.0] * 4


def test_locks_exist_on_a_real_daemon(tmp_path, monkeypatch):
    """The real constructor must create WORKING locks, not just mention their
    names somewhere in __init__.

    The old assertion inspected `__init__.__code__.co_names`, which passes as
    long as the attribute name is referenced ANYWHERE in __init__'s bytecode --
    including e.g. a stray comment-adjacent reference or an attribute set to
    None -- without proving a real, functioning lock was ever constructed.
    This constructs a REAL Daemon and exercises each lock as a mutex: a
    non-blocking acquire must succeed (proving it starts unlocked and is a
    real Lock-like object with acquire/release), and a second non-blocking
    acquire while the first is held must fail (proving it actually excludes).
    """
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    store = Store(tmp_path / "s.sqlite3", dim=4)
    store.init()
    dm = d.Daemon(store, _FakeEmb(), services={},
                  lock=d.SingleWriterLock(tmp_path / "d.lock"))
    for name in ("_stash_lock", "_embedder_lock", "_bulk_lock"):
        lock = getattr(dm, name, None)
        assert lock is not None, f"{name} was never set on a real Daemon"
        assert lock.acquire(blocking=False), f"{name} is not a working, unlocked Lock"
        try:
            assert not lock.acquire(blocking=False), (
                f"{name} did not exclude a second acquire -- not a real mutex")
        finally:
            lock.release()


# ---------------------------------------------------------------------------
# _stash_lock must actually be ACQUIRED, not merely created (Task 8, Step 1
# review round 2): the brief's literal complaint about the old co_names-based
# test was "it passes if the locks are created but never used". The
# behavioural rewrite above (test_locks_exist_on_a_real_daemon) proves each
# lock is a real, working mutex in isolation, but does not prove
# _stash_snapshot/_stash_clear_drained actually TAKE it -- a reviewer
# confirmed replacing all `with self._stash_lock:` sites in daemon.py with
# `if True:` still left that test (and the whole suite) green. These two
# tests close that gap directly: hold the REAL _stash_lock in one thread and
# time how long _stash_snapshot()/_stash_clear_drained() take to return on
# another -- if the lock is genuinely acquired, the call must block for
# close to the hold duration; if it were neutered, it returns almost
# instantly regardless of the held lock (reproduced directly below).
# ---------------------------------------------------------------------------

def _hold_stash_lock(dm, hold_s, acquired_evt, release_evt):
    def _hold():
        with dm._stash_lock:
            acquired_evt.set()
            release_evt.wait(timeout=hold_s + 5)
    t = threading.Thread(target=_hold, daemon=True)
    t.start()
    assert acquired_evt.wait(timeout=2.0), "holder never acquired _stash_lock"
    return t


def test_stash_snapshot_actually_blocks_on_a_held_stash_lock():
    dm = d.Daemon.__new__(d.Daemon)
    dm._stash_lock = threading.Lock()
    dm._pending_blocks = {}
    dm._pending_audit = {}
    dm._pending_synthesis = []
    dm._stash_generation = {}

    hold_s = 0.3
    acquired = threading.Event()
    release = threading.Event()
    holder = _hold_stash_lock(dm, hold_s, acquired, release)

    def _release_after_a_while():
        time.sleep(hold_s)
        release.set()
    threading.Thread(target=_release_after_a_while, daemon=True).start()

    started = time.monotonic()
    dm._stash_snapshot()
    elapsed = time.monotonic() - started

    holder.join(timeout=5.0)
    assert not holder.is_alive()

    assert elapsed >= hold_s * 0.8, (
        f"_stash_snapshot returned after only {elapsed:.2f}s while a real "
        f"held _stash_lock should have blocked it for close to {hold_s}s -- "
        f"the lock is not actually being acquired"
    )


def test_stash_clear_drained_actually_blocks_on_a_held_stash_lock():
    dm = d.Daemon.__new__(d.Daemon)
    dm._stash_lock = threading.Lock()
    dm._pending_blocks = {"k": ["old"]}
    dm._pending_audit = {}
    dm._pending_synthesis = []
    dm._stash_generation = {"k": 1}
    taken = {"blocks": {"k": ["old"]}, "audit": {}, "synthesis": [], "gen": {"k": 1}}

    hold_s = 0.3
    acquired = threading.Event()
    release = threading.Event()
    holder = _hold_stash_lock(dm, hold_s, acquired, release)

    def _release_after_a_while():
        time.sleep(hold_s)
        release.set()
    threading.Thread(target=_release_after_a_while, daemon=True).start()

    started = time.monotonic()
    dm._stash_clear_drained({"k_drained": 1}, taken)
    elapsed = time.monotonic() - started

    holder.join(timeout=5.0)
    assert not holder.is_alive()

    assert elapsed >= hold_s * 0.8, (
        f"_stash_clear_drained returned after only {elapsed:.2f}s while a "
        f"real held _stash_lock should have blocked it for close to "
        f"{hold_s}s -- the lock is not actually being acquired"
    )


def test_pending_update_stops_the_maintenance_thread():
    """Breaking out of run() for an update releases SingleWriterLock; if the
    maintenance thread is still writing, the successor process makes two
    writers — exactly what the file lock exists to prevent."""
    import threading
    from mcpbrain import daemon as d
    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    dm._maintenance_thread = threading.Thread(target=dm._stop.wait, daemon=True)
    dm._maintenance_thread.start()
    dm._shutdown_maintenance()
    assert dm._stop.is_set()
    assert not dm._maintenance_thread.is_alive()


def test_shutdown_maintenance_logs_when_thread_survives_the_join_timeout(caplog):
    """Important finding, final whole-branch review: the old join(timeout=...)
    had no is_alive() check/log/escalation afterward. Since gated-pass
    execution is deliberately unbounded once _bulk_lock is acquired (only the
    ACQUIRE is bounded -- see BULK_LOCK_ACQUIRE_S), a mid-pass maintenance
    thread surviving the join is a real, reachable case: run()'s
    _pending_update path still proceeds to release SingleWriterLock right
    after this returns, regardless, reopening the two-writers hazard this
    method exists to prevent. This drives a thread that genuinely does NOT
    stop within the timeout (blocks on an Event that is never set) and
    asserts the escalation is logged, not silently swallowed.

    The existing test above (test_pending_update_stops_the_maintenance_thread)
    does not cover this: its thread target (_stop.wait) returns the instant
    _stop is set, so the join always succeeds trivially and the escalation
    branch is never entered.
    """
    import logging
    import threading
    from mcpbrain import daemon as d

    dm = d.Daemon.__new__(d.Daemon)
    dm._stop = threading.Event()
    never = threading.Event()   # deliberately never set -- the thread outlives the join
    dm._maintenance_thread = threading.Thread(target=never.wait, daemon=True)
    dm._maintenance_thread.start()

    with caplog.at_level(logging.ERROR, logger=d.log.name):
        dm._shutdown_maintenance(timeout=0.05)

    assert dm._stop.is_set()
    assert dm._maintenance_thread.is_alive(), (
        "precondition: the thread must genuinely still be running after the "
        "join timed out, or this test proves nothing"
    )
    assert any(
        r.levelno >= logging.ERROR and "did not stop" in r.message
        for r in caplog.records
    ), "expected an ERROR log naming the thread's failure to stop in time"

    # Teardown: let the still-alive thread exit cleanly so it doesn't leak
    # past this test.
    never.set()
    dm._maintenance_thread.join(timeout=5.0)
    assert not dm._maintenance_thread.is_alive()


def test_stash_delete_does_not_drop_a_fresh_batch():
    """run_one snapshots, the cycle runs, then it deletes drained keys. If a
    pass rewrote that key meanwhile, the fresh batch is deleted unattached."""
    import threading
    from mcpbrain import daemon as d
    dm = d.Daemon.__new__(d.Daemon)
    dm._stash_lock = threading.Lock()
    dm._pending_blocks = {"k": ["old"]}
    dm._pending_audit = {}
    dm._pending_synthesis = []
    dm._stash_generation = {"k": 1}
    taken = dm._stash_snapshot()
    dm._pending_blocks["k"] = ["fresh"]          # maintenance thread rewrites
    dm._stash_generation["k"] = 2
    dm._stash_clear_drained({"k_drained": 1}, taken)
    assert dm._pending_blocks.get("k") == ["fresh"], "fresh batch was dropped"


def test_stash_delete_clears_an_unchanged_key():
    """Sanity check on the other side of the generation-safety fix: a key that
    was NOT rewritten since the snapshot must still be cleared once drained --
    otherwise every stash would leak forever and never actually clear."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._stash_lock = threading.Lock()
    dm._pending_blocks = {"k": ["old"]}
    dm._pending_audit = {}
    dm._pending_synthesis = []
    dm._stash_generation = {"k": 1}
    taken = dm._stash_snapshot()
    # Nothing rewrites "k" this time.
    dm._stash_clear_drained({"k_drained": 1}, taken)
    assert "k" not in dm._pending_blocks, "unchanged, drained key must be cleared"


def test_stash_snapshot_and_clear_handle_synthesis_the_same_way():
    """_pending_synthesis is list-shaped, not dict-shaped like blocks/audit, but
    it has the identical mid-cycle-rewrite race: a fresh synthesise pass must
    not be wiped by a drain result that answered the OLD batch."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._stash_lock = threading.Lock()
    dm._pending_blocks = {}
    dm._pending_audit = {}
    dm._pending_synthesis = ["old"]
    dm._stash_generation = {dm._SYNTHESIS_GEN_KEY: 1}
    taken = dm._stash_snapshot()
    dm._pending_synthesis = ["fresh"]
    dm._stash_generation[dm._SYNTHESIS_GEN_KEY] = 2
    dm._stash_clear_drained({"synthesis_written": True}, taken)
    assert dm._pending_synthesis == ["fresh"], "fresh synthesis batch was dropped"


def test_embedder_model_access_is_not_serialised():
    """Concurrent embed_passages calls MUST be allowed to overlap inside the
    model.

    This used to be test_embedder_lock_serialises_model_access, asserting the
    OPPOSITE (that access never overlaps) -- correct for the per-instance lock
    _LocalEmbedder used to hold across embed_passages/embed_query, but that
    lock was removed (Task 6, Step 3 of the 2026-07-27 daemon-scheduling
    remediation): it was serialising /api/recall's embed_query behind a bulk
    embed_passages batch, the opposite of a stated goal. That old assertion
    was also non-deterministic-by-luck rather than a real guarantee: with no
    sleep/IO point inside the fake model's critical section, the GIL rarely
    preempted mid-call, so the old test kept passing even after the lock was
    removed -- it was measuring GIL scheduling luck, not the lock. This
    version forces a real overlap window (a short sleep while "inside" the
    model) so the assertion is deterministic either way.
    """
    overlaps = []
    inside = threading.Lock()
    active = [0]

    class _Model:
        def embed(self, texts):
            with inside:
                active[0] += 1
                overlapped = active[0] > 1
            time.sleep(0.05)  # force a real window for another thread to enter
            if overlapped:
                overlaps.append(True)
            try:
                return [[0.0] * 4 for _ in texts]
            finally:
                with inside:
                    active[0] -= 1

    from mcpbrain.embed import _LocalEmbedder
    emb = _LocalEmbedder.__new__(_LocalEmbedder)
    emb._model = _Model()
    emb.dim = 4
    emb._qp = ""
    emb._lock = threading.Lock()  # matches the __new__-construction shape; unused by embed_passages

    threads = [threading.Thread(target=lambda: emb.embed_passages(["x"] * 4))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    assert overlaps, "expected concurrent entry into the model (no serialising lock)"


def test_embedder_bounded_returns_immediately_once_built():
    dm = d.Daemon.__new__(d.Daemon)
    dm._embedder_obj = object()
    dm._embedder_lock = threading.Lock()
    assert dm._embedder_bounded() is dm._embedder_obj


def test_embedder_bounded_skips_rather_than_blocking_on_a_build_in_flight():
    """_run_self_improve (non-gated) must not park the whole maintenance tick
    behind someone else's in-flight cold model download."""
    dm = d.Daemon.__new__(d.Daemon)
    dm._embedder_obj = None
    dm._embedder_lock = threading.Lock()
    dm._embedder_lock.acquire()      # simulate a build already in progress
    try:
        assert dm._embedder_bounded(timeout=0.05) is None
    finally:
        dm._embedder_lock.release()


def test_embedder_bounded_builds_when_free():
    dm = d.Daemon.__new__(d.Daemon)
    dm._embedder_obj = None
    dm._embedder_lock = threading.Lock()
    built = object()
    dm._embedder_factory = lambda: built
    dm._model_building = False
    assert dm._embedder_bounded(timeout=1.0) is built
    assert dm._embedder_obj is built
