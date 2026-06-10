import json, threading, time
from mcpbrain.store import Store
from mcpbrain.daemon import Daemon, SingleWriterLock


class _Emb:
    dim = 4
    def embed_passages(self, t): return [[0.0]*4 for _ in t]


def _daemon(tmp_path, monkeypatch):
    (tmp_path/"config.json").write_text(json.dumps(
        {"owner_name":"S","owner_email":"s@x","orgs":[{"name":"O"}]}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = Store(tmp_path/"b.sqlite3", dim=4, read_only=False); s.init()
    return Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path/"d.lock"))


def test_second_start_is_noop_while_running(tmp_path, monkeypatch):
    import mcpbrain.enrich_backfill as eb
    started = {"n": 0}
    release = threading.Event()
    def fake_run(**kw):
        started["n"] += 1
        release.wait(2)
    monkeypatch.setattr(eb, "run_backfill", fake_run)
    d = _daemon(tmp_path, monkeypatch)
    d.start_enrich_backfill(); time.sleep(0.1)
    d.start_enrich_backfill()  # second start should be a no-op
    release.set(); time.sleep(0.2)
    assert started["n"] == 1


def test_run_one_skips_while_backfill_active(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    d._backfill_active.set()
    assert d.run_one() is None
