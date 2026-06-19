"""Absolute off-topic gate for recall: daemon.search suppresses queries whose
nearest brain chunk is past recall_max_distance, and ranks the rest normally."""
import json

import pytest

import mcpbrain.daemon as daemon_mod
from mcpbrain.daemon import Daemon, SingleWriterLock
from mcpbrain import config


class _FakeStore:
    def __init__(self, knn):
        self._knn = knn

    def vec_knn(self, qv, k):
        return self._knn[:k]


class _FakeEmbedder:
    dim = 4

    def embed_query(self, text):
        return [0.0, 0.0, 0.0, 0.0]


def _daemon(tmp_path, knn):
    return Daemon(_FakeStore(knn), _FakeEmbedder(), services={},
                  lock=SingleWriterLock(tmp_path / "d.lock"))


def test_recall_max_distance_default_and_override(tmp_path):
    assert config.recall_max_distance(str(tmp_path)) == 0.80
    (tmp_path / "config.json").write_text(json.dumps({"recall_max_distance": 1.05}))
    assert config.recall_max_distance(str(tmp_path)) == 1.05


def test_gate_suppresses_off_topic(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    # nearest chunk past the 0.80 default -> off-topic -> [] and no ranking work
    d = _daemon(tmp_path, knn=[("d1", 0.95), ("d2", 1.1)])
    called = {"n": 0}
    monkeypatch.setattr(daemon_mod, "hybrid_search",
                        lambda *a, **k: called.update(n=called["n"] + 1) or [])
    assert d.search("totally unrelated") == []
    assert called["n"] == 0  # gate short-circuited before hybrid_search


def test_gate_passes_on_topic_and_attaches_distance(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d = _daemon(tmp_path, knn=[("d1", 0.62), ("d2", 0.70)])
    monkeypatch.setattr(daemon_mod, "hybrid_search",
                        lambda *a, **k: [{"doc_id": "d1", "score": 1.0, "text": "hit"}])
    out = d.search("on topic query")
    assert out and out[0]["doc_id"] == "d1"
    assert out[0]["distance"] == 0.62
    assert out[0]["score"] == 1.0


def test_gate_respects_config_override(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    (tmp_path / "config.json").write_text(json.dumps({"recall_max_distance": 1.2}))
    d = _daemon(tmp_path, knn=[("d1", 0.95)])
    monkeypatch.setattr(daemon_mod, "hybrid_search",
                        lambda *a, **k: [{"doc_id": "d1", "score": 1.0, "text": "hit"}])
    assert d.search("q")  # 0.95 < 1.2 -> now passes


def test_gate_empty_knn_returns_empty(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d = _daemon(tmp_path, knn=[])
    assert d.search("q") == []


def test_search_returns_empty_on_embed_failure(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))

    class _BoomEmbedder:
        dim = 4
        def embed_query(self, text):
            raise RuntimeError("embed blew up")

    d = Daemon(_FakeStore([("d1", 0.1)]), _BoomEmbedder(), services={},
               lock=SingleWriterLock(tmp_path / "d.lock"))
    assert d.search("q") == []  # never raises into the prompt path


def test_hybrid_search_reuses_query_vec(monkeypatch):
    # query_vec short-circuits the embed_query call inside hybrid_search.
    from mcpbrain import retrieval

    class _Emb:
        def embed_query(self, text):
            raise AssertionError("embed_query must not be called when query_vec is given")

    class _Store:
        def vec_knn(self, qv, k):
            return []
        def fts_search(self, q, k):
            return []

    out = retrieval.hybrid_search(_Store(), _Emb(), "q", 5, query_vec=[0.0, 0.0, 0.0, 0.0])
    assert out == []
