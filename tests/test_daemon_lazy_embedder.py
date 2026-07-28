import threading
import time

from mcpbrain.daemon import Daemon


class _FakeStore:
    def __init__(self): self.dim = 384
    def init(self): pass


def _make_daemon(factory):
    d = Daemon.__new__(Daemon)          # bypass full __init__ wiring
    d._embedder_obj = None
    d._embedder_factory = factory
    d._embedder_lock = threading.Lock()
    return d


def test_embedder_not_built_until_accessed():
    calls = []
    d = _make_daemon(lambda: (calls.append(1), "EMB")[1])
    assert calls == []                  # constructing did not build
    assert d._embedder == "EMB"         # first access builds
    assert d._embedder == "EMB"         # memoised
    assert calls == [1]                 # built exactly once


def test_embedder_missing_factory_raises_on_access():
    import pytest
    d = _make_daemon(None)
    with pytest.raises(RuntimeError):
        _ = d._embedder


def test_search_returns_empty_when_embedder_unavailable():
    d = _make_daemon(None)              # accessing _embedder raises
    d._store = _FakeStore()
    assert d.search("anything", 5) == []


def test_migrate_embed_backend_safe_swallows_network_error():
    """A model-build/download failure (e.g. offline machine hitting the
    network while fetching bge-small) must degrade to skip-and-continue,
    not propagate out of run()'s pre-loop migrate call. The old guard
    (`except RuntimeError`) let this through; the extracted method must not."""
    d = Daemon.__new__(Daemon)
    d.migrate_embed_backend = lambda: (_ for _ in ()).throw(OSError("network down"))
    d._migrate_embed_backend_safe()     # must not raise


def test_migrate_embed_backend_safe_swallows_runtime_error():
    """Still covers the original case: embedder has no factory (lazy, not
    yet built) raises RuntimeError, which must also be swallowed."""
    d = Daemon.__new__(Daemon)
    d.migrate_embed_backend = lambda: (_ for _ in ()).throw(RuntimeError("no factory"))
    d._migrate_embed_backend_safe()     # must not raise


# ---------------------------------------------------------------------------
# Daemon._embedder's double-checked locking must actually be single-flight
# under concurrency (Task 8, Step 2). test_embedder_not_built_until_accessed
# above only exercises single-threaded access (build-once/memoise); nothing
# in the suite races multiple threads through the property the way
# test_embed_locking.py's test_get_embedder_build_is_single_flight_under_
# concurrency does for the module-level get_embedder's own double-checked
# locking.
# ---------------------------------------------------------------------------

def test_embedder_property_build_is_single_flight_under_concurrency():
    build_count = {"n": 0}
    count_lock = threading.Lock()

    def _slow_factory():
        with count_lock:
            build_count["n"] += 1
        time.sleep(0.2)  # widen the race window so racing threads overlap
        return object()

    d = _make_daemon(_slow_factory)

    results: list = []
    results_lock = threading.Lock()

    def worker():
        e = d._embedder
        with results_lock:
            results.append(e)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5)
    assert not any(t.is_alive() for t in threads)

    assert build_count["n"] == 1, (
        f"expected exactly one factory build, got {build_count['n']} -- "
        f"concurrent _embedder accesses raced past the double-checked lock"
    )
    assert len(results) == 10
    assert len({id(r) for r in results}) == 1, (
        "every concurrent caller must get back the SAME embedder instance"
    )
