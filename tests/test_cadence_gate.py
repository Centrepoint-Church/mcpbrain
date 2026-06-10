import json
from mcpbrain.store import Store
from mcpbrain.daemon import Daemon, SingleWriterLock

class _Emb:
    dim = 4
    def embed_passages(self, t): return [[0.0]*4 for _ in t]

def _daemon(tmp_path, configured, monkeypatch):
    cfg = {"owner_name":"S","owner_email":"s@x","orgs":[{"name":"O"}]} if configured else {}
    (tmp_path/"config.json").write_text(json.dumps(cfg))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = Store(tmp_path/"b.sqlite3", dim=4, read_only=False); s.init()
    return Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path/"d.lock"),
                  communities_interval_s=1.0, clock=lambda: 1e9)

def test_graph_writers_skipped_when_unconfigured(tmp_path, monkeypatch):
    d = _daemon(tmp_path, configured=False, monkeypatch=monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(d, "maybe_communities", lambda: called.__setitem__("n", called["n"]+1))
    d._run_periodic_passes()
    assert called["n"] == 0

def test_graph_writers_run_when_configured(tmp_path, monkeypatch):
    d = _daemon(tmp_path, configured=True, monkeypatch=monkeypatch)
    called = {"n": 0}
    monkeypatch.setattr(d, "maybe_communities", lambda: called.__setitem__("n", called["n"]+1))
    d._run_periodic_passes()
    assert called["n"] == 1
