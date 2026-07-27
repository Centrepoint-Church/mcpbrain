"""Recall must not queue behind a bulk embedding batch."""
import threading
import time

from mcpbrain.embed import _LocalEmbedder


def test_embed_query_is_not_blocked_by_a_long_passage_batch():
    emb = _LocalEmbedder.__new__(_LocalEmbedder)
    emb.dim = 4
    emb._qp = ""
    emb._lock = threading.Lock()

    class _Model:
        def embed(self, texts):
            time.sleep(0.5)                      # a bulk batch
            return [[0.0] * 4 for _ in texts]

        def query_embed(self, texts):
            return iter([[0.0] * 4])

    emb._model = _Model()
    t = threading.Thread(target=lambda: emb.embed_passages(["x"] * 4), daemon=True)
    t.start()
    time.sleep(0.05)
    start = time.monotonic()
    emb.embed_query("hello")
    elapsed = time.monotonic() - start
    t.join(timeout=5)
    assert elapsed < 0.3, f"embed_query waited {elapsed:.2f}s behind the batch"


# ---------------------------------------------------------------------------
# get_embedder's build must be single-flight (regression test for a race
# found beyond the brief's original scope while investigating this file).
#
# functools.lru_cache does NOT serialise concurrent cache misses: N threads
# racing a cold call each fully execute the wrapped function, so removing
# the per-call lock above (this class's real fix) would have re-exposed a
# SEPARATE bug if get_embedder had been left on lru_cache -- two threads
# calling get_embedder() before either finishes could each build their own
# _LocalEmbedder (each downloading/loading the ONNX model and writing the
# same on-disk fastembed cache files concurrently). get_embedder was
# rewritten as an explicit dict + lock (double-checked locking) instead; this
# pins that down so a future "simplify this back to @lru_cache" doesn't
# silently reintroduce the race.
# ---------------------------------------------------------------------------

def test_get_embedder_build_is_single_flight_under_concurrency(monkeypatch):
    from mcpbrain import embed

    embed._EMBEDDER_CACHE.clear()  # defensive: a prior test may have populated it
    build_count = {"n": 0}
    count_lock = threading.Lock()

    class _SlowFakeEmbedder:
        def __init__(self, *a, **kw):
            with count_lock:
                build_count["n"] += 1
            time.sleep(0.2)  # simulate a slow ONNX model load/download

    monkeypatch.setattr(embed, "_LocalEmbedder", _SlowFakeEmbedder)

    results: list = []
    results_lock = threading.Lock()

    def worker():
        e = embed.get_embedder("bge-small")
        with results_lock:
            results.append(e)

    try:
        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5)
        assert not any(t.is_alive() for t in threads)

        assert build_count["n"] == 1, (
            f"expected exactly one _LocalEmbedder build, got {build_count['n']} "
            f"-- concurrent get_embedder() calls raced past each other"
        )
        assert len(results) == 10
        assert len({id(r) for r in results}) == 1, (
            "every concurrent caller must get back the SAME embedder instance"
        )
    finally:
        embed._EMBEDDER_CACHE.clear()  # don't leak the fake into other tests


# ---------------------------------------------------------------------------
# OMP_NUM_THREADS must be set BEFORE `from fastembed import TextEmbedding`
# runs (post-review fix: an earlier version checked sys.modules AFTER that
# import, which is exactly what inserts "fastembed"/"onnxruntime" into
# sys.modules in the first place -- so the guard's condition was always False
# and the assignment silently never happened).
#
# Uses a real import-system hook (sys.meta_path) rather than pre-populating
# sys.modules, because pre-populating it would make "fastembed" look
# already-imported from the very start of the test -- which is a DIFFERENT,
# legitimate case the guard is supposed to skip. The bug only shows up when
# the *statement* `from fastembed import TextEmbedding` is what performs the
# fresh import (and its sys.modules side effect) -- so the test must let that
# statement do a real (faked) import and observe environment state at the
# exact moment it happens.
# ---------------------------------------------------------------------------

def test_omp_num_threads_is_set_before_the_fastembed_import_runs(monkeypatch):
    import importlib.abc
    import importlib.machinery
    import os
    import sys
    import types

    for name in list(sys.modules):
        if name == "fastembed" or name.startswith("fastembed."):
            monkeypatch.delitem(sys.modules, name, raising=False)
    monkeypatch.delitem(sys.modules, "onnxruntime", raising=False)
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)

    seen = {}

    class _FakeLoader(importlib.abc.Loader):
        def create_module(self, spec):
            # This runs exactly when `from fastembed import TextEmbedding`
            # performs its first real import -- the same moment the real bug
            # hinges on (sys.modules gaining "fastembed" as a side effect of
            # this very statement).
            seen["omp_num_threads_at_import"] = os.environ.get("OMP_NUM_THREADS")
            mod = types.ModuleType("fastembed")
            mod.TextEmbedding = lambda **kw: types.SimpleNamespace()
            return mod

        def exec_module(self, module):
            pass

    class _FakeFinder(importlib.abc.MetaPathFinder):
        def find_spec(self, name, path, target=None):
            if name == "fastembed":
                return importlib.machinery.ModuleSpec(name, _FakeLoader())
            return None

    finder = _FakeFinder()
    sys.meta_path.insert(0, finder)
    try:
        from mcpbrain.embed import _LocalEmbedder
        _LocalEmbedder("BAAI/bge-small-en-v1.5", 384, "prefix ")
    finally:
        sys.meta_path.remove(finder)

    assert seen.get("omp_num_threads_at_import") == "1", (
        "OMP_NUM_THREADS must already be '1' by the time fastembed is "
        "imported -- if this is None, the guard runs AFTER the import and "
        "silently never fires (the exact regression this test catches)"
    )
