"""State shared between the cycle loop and the maintenance thread.

These are real-thread tests on purpose: the whole bug class lives in the
interleaving, and a mocked lock proves nothing.
"""
import threading

from mcpbrain import daemon as d


def test_locks_exist_on_a_real_daemon(tmp_path):
    """The real constructor must create all three locks."""
    for name in ("_stash_lock", "_embedder_lock", "_bulk_lock"):
        assert name in d.Daemon.__init__.__code__.co_names, f"{name} not set in __init__"


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


def test_embedder_lock_serialises_model_access():
    """Two threads embedding concurrently must not overlap inside the model."""
    overlaps = []
    inside = threading.Lock()
    active = [0]

    class _Model:
        def embed(self, texts):
            with inside:
                active[0] += 1
                if active[0] > 1:
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
    emb._lock = threading.Lock()

    threads = [threading.Thread(target=lambda: emb.embed_passages(["x"] * 50))
               for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    assert overlaps == [], "embedder model was entered concurrently"


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
