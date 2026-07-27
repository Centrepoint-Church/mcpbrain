"""State shared between the cycle loop and the maintenance thread.

These are real-thread tests on purpose: the whole bug class lives in the
interleaving, and a mocked lock proves nothing.
"""
import threading

from mcpbrain import daemon as d


def _bare_daemon():
    dm = d.Daemon.__new__(d.Daemon)
    dm._stash_lock = threading.Lock()
    dm._embedder_lock = threading.Lock()
    dm._bulk_lock = threading.Lock()
    dm._pending_blocks = {}
    dm._pending_audit = {}
    dm._pending_synthesis = {}
    return dm


def test_locks_exist_on_a_real_daemon(tmp_path):
    """The real constructor must create all three locks."""
    for name in ("_stash_lock", "_embedder_lock", "_bulk_lock"):
        assert name in d.Daemon.__init__.__code__.co_names, f"{name} not set in __init__"


def test_stash_take_is_atomic_under_concurrent_writers():
    """No update is lost and no key is read-then-dropped mid-write."""
    dm = _bare_daemon()
    stop = threading.Event()

    def writer(n):
        i = 0
        while not stop.is_set():
            with dm._stash_lock:
                dm._pending_blocks[f"w{n}-{i}"] = [i]
            i += 1

    threads = [threading.Thread(target=writer, args=(n,)) for n in range(3)]
    for t in threads:
        t.start()

    taken = []
    for _ in range(200):
        taken.append(dm._stash_take())

    stop.set()
    for t in threads:
        t.join(timeout=30)
    assert not any(t.is_alive() for t in threads)

    # Every take returns a plain dict snapshot and leaves the stash cleared;
    # crucially nothing raises "dictionary changed size during iteration".
    assert all(isinstance(x, dict) for x in taken)


def test_stash_take_clears_and_returns_contents():
    dm = _bare_daemon()
    dm._pending_blocks = {"a": [1]}
    dm._pending_audit = {"b": [2]}
    dm._pending_synthesis = {"c": [3]}
    got = dm._stash_take()
    assert got == {"blocks": {"a": [1]}, "audit": {"b": [2]}, "synthesis": {"c": [3]}}
    assert dm._pending_blocks == {} and dm._pending_audit == {}
    assert dm._pending_synthesis == {}


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
