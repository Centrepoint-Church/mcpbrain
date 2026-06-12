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
    # Patch _run_communities — the dispatch table calls _run_X directly.
    monkeypatch.setattr(d, "_run_communities", lambda: called.__setitem__("n", called["n"]+1))
    d._run_periodic_passes()
    assert called["n"] == 0

def test_graph_writers_run_when_configured(tmp_path, monkeypatch):
    d = _daemon(tmp_path, configured=True, monkeypatch=monkeypatch)
    called = {"n": 0}
    # Patch _run_communities — the dispatch table calls _run_X directly.
    monkeypatch.setattr(d, "_run_communities", lambda: called.__setitem__("n", called["n"]+1))
    d._run_periodic_passes()
    assert called["n"] == 1

def test_update_and_verify_run_even_when_unconfigured(tmp_path, monkeypatch):
    d = _daemon(tmp_path, configured=False, monkeypatch=monkeypatch)
    calls = {"au": 0, "vc": 0}
    # Patch _run_X methods — the dispatch table calls these directly.
    monkeypatch.setattr(d, "_run_auto_update", lambda: calls.__setitem__("au", calls["au"]+1))
    monkeypatch.setattr(d, "_run_verify", lambda: calls.__setitem__("vc", calls["vc"]+1))
    d._run_periodic_passes()
    assert calls["au"] == 1 and calls["vc"] == 1  # exemptions fire regardless of config
