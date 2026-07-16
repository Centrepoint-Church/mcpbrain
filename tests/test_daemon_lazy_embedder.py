from mcpbrain.daemon import Daemon


class _FakeStore:
    def __init__(self): self.dim = 384
    def init(self): pass


def _make_daemon(factory):
    d = Daemon.__new__(Daemon)          # bypass full __init__ wiring
    d._embedder_obj = None
    d._embedder_factory = factory
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
