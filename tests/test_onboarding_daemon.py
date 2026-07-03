from mcpbrain import daemon as d
from mcpbrain.daemon import Daemon
from mcpbrain.store import Store


class _Emb:
    def embed_query(self, q): return [0.0, 0.0, 0.0, 0.0]
    def embed_documents(self, xs): return [[0.0] * 4 for _ in xs]


def _daemon(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4); s.init()
    return Daemon(s, _Emb(), services={"drive_service": object()})


def test_bootstrap_runs_once_then_noops(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(d.onboarding, "should_bootstrap", lambda home: True)
    calls = []
    monkeypatch.setattr(d.onboarding, "run_bootstrap",
                        lambda home, store, **kw: calls.append(kw) or {"status": "done"})
    dm = _daemon(tmp_path)
    assert dm.bootstrap_baseline_once() == {"status": "done"}
    assert dm.bootstrap_baseline_once() is None      # in-proc flag -> no-op
    assert len(calls) == 1
    # the drive_service from services is forwarded to run_bootstrap
    assert "drive_service" in calls[0]


def test_degraded_does_not_set_done_flag(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(d.onboarding, "should_bootstrap", lambda home: True)
    n = {"i": 0}

    def _run(home, store, **kw):
        n["i"] += 1
        return {"status": "degraded"}
    monkeypatch.setattr(d.onboarding, "run_bootstrap", _run)
    dm = _daemon(tmp_path)
    dm.bootstrap_baseline_once()
    dm.bootstrap_baseline_once()                     # degraded -> retried
    assert n["i"] == 2


def test_gate_skips_when_should_bootstrap_false(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(d.onboarding, "should_bootstrap", lambda home: False)
    called = []
    monkeypatch.setattr(d.onboarding, "run_bootstrap",
                        lambda *a, **k: called.append(1) or {"status": "done"})
    dm = _daemon(tmp_path)
    assert dm.bootstrap_baseline_once() is None
    assert called == []


def test_never_raises_into_cycle(tmp_path, monkeypatch):
    monkeypatch.setattr(d, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(d.onboarding, "should_bootstrap", lambda home: True)

    def _boom(*a, **k):
        raise RuntimeError("kaboom")
    monkeypatch.setattr(d.onboarding, "run_bootstrap", _boom)
    dm = _daemon(tmp_path)
    res = dm.bootstrap_baseline_once()               # must not raise
    assert res["status"] == "error"
