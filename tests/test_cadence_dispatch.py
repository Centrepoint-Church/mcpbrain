"""Tests for the CadencePass dispatch table in _run_periodic_passes."""
import json
from mcpbrain.daemon import Daemon, SingleWriterLock
from mcpbrain.store import Store


class _FakeEmbedder:
    dim = 4
    def embed(self, texts):
        import numpy as np
        return np.zeros((len(texts), self.dim), dtype="float32")


class _Clock:
    def __init__(self, t=0.0): self.t = t
    def __call__(self): return self.t


def _configured_daemon(tmp_path, **kw):
    (tmp_path / "config.json").write_text(json.dumps(
        {"owner_name": "A", "owner_email": "a@x.com", "orgs": [{"name": "O"}]}))
    import os; os.environ["MCPBRAIN_HOME"] = str(tmp_path)
    store = Store(tmp_path / "d.sqlite3", dim=4); store.init()
    clock = _Clock()
    d = Daemon(store, _FakeEmbedder(), services={},
               lock=SingleWriterLock(tmp_path / "d.lock"), clock=clock, **kw)
    return d, clock


def test_dispatch_table_pass_fires_when_due(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _configured_daemon(tmp_path, communities_interval_s=100.0)
    fired = []
    monkeypatch.setattr("mcpbrain.communities.run", lambda store: fired.append(1) or {"communities": 1})
    d._run_periodic_passes()
    assert len(fired) == 1


def test_dispatch_table_pass_skipped_when_not_due(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _configured_daemon(tmp_path, communities_interval_s=100.0)
    fired = []
    monkeypatch.setattr("mcpbrain.communities.run", lambda store: fired.append(1) or {"communities": 1})
    d._run_periodic_passes(); d._run_periodic_passes()
    assert len(fired) == 1


def test_dispatch_table_pass_refires_after_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _configured_daemon(tmp_path, communities_interval_s=100.0)
    fired = []
    monkeypatch.setattr("mcpbrain.communities.run", lambda store: fired.append(1) or {"communities": 1})
    d._run_periodic_passes(); clock.t = 101.0; d._run_periodic_passes()
    assert len(fired) == 2


def test_dispatch_table_backfill_suppresses_all_graph_passes(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _configured_daemon(tmp_path, communities_interval_s=1.0)
    fired = []
    monkeypatch.setattr("mcpbrain.communities.run", lambda store: fired.append(1) or {})
    d._backfill_active.set()
    d._run_periodic_passes()
    assert fired == []
