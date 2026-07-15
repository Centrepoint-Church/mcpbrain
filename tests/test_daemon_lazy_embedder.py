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
