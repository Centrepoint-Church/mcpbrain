import json
from unittest.mock import patch
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


def _daemon(tmp_path, extra=None):
    cfg = {"owner_name": "A", "owner_email": "a@x.com", "orgs": [{"name": "O"}]}
    if extra: cfg.update(extra)
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    import os; os.environ["MCPBRAIN_HOME"] = str(tmp_path)
    store = Store(tmp_path / "cu.sqlite3", dim=4); store.init()
    clock = _Clock()
    d = Daemon(store, _FakeEmbedder(), services={},
               lock=SingleWriterLock(tmp_path / "d.lock"), clock=clock)
    return d, clock


def test_clickup_inactive_without_key_or_list(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, _ = _daemon(tmp_path)
    assert d.maybe_clickup_sync() is None

def test_clickup_inactive_with_key_only(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, _ = _daemon(tmp_path, {"clickup_api_key": "pk_x"})
    assert d.maybe_clickup_sync() is None

def test_clickup_active_with_key_and_list(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, _ = _daemon(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})
    with patch("mcpbrain.clickup_sync.sync", return_value={"synced": 1}) as m:
        assert d.maybe_clickup_sync() == {"synced": 1}
    m.assert_called_once()

def test_clickup_respects_fixed_interval(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d, clock = _daemon(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})
    with patch("mcpbrain.clickup_sync.sync", return_value={}):
        assert d.maybe_clickup_sync() == {}
    clock.t = 299.0
    with patch("mcpbrain.clickup_sync.sync", return_value={"x": 1}) as m2:
        assert d.maybe_clickup_sync() is None
    m2.assert_not_called()
    clock.t = 301.0
    with patch("mcpbrain.clickup_sync.sync", return_value={"x": 1}) as m3:
        assert d.maybe_clickup_sync() == {"x": 1}
    m3.assert_called_once()
