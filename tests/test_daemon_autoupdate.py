from mcpbrain.store import Store
from mcpbrain.daemon import Daemon, SingleWriterLock


class _Emb:
    dim = 4
    def embed_passages(self, texts): return [[0.0] * 4 for _ in texts]


def _daemon(tmp_path, **kw):
    s = Store(tmp_path / "b.sqlite3", dim=4, read_only=False); s.init()
    clock = kw.pop("clock", lambda: 0.0)
    return Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path / "d.lock"),
                  clock=clock, **kw)


def test_auto_update_off_by_default(tmp_path):
    d = _daemon(tmp_path)
    assert d.maybe_auto_update() is None  # OFF unless interval set


def test_auto_update_runs_when_due_and_behind(tmp_path, monkeypatch):
    import mcpbrain.update as upd
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upd, "_latest_version", lambda url: "0.3.0")
    ran = {"n": 0}
    monkeypatch.setattr(upd, "update_from_index", lambda url: ran.__setitem__("n", 1) or 0)
    d = _daemon(tmp_path, auto_update_interval_s=3600.0)
    out = d.maybe_auto_update()  # first call: due (last is None)
    assert ran["n"] == 1 and out is not None and out.get("updated") is True


def test_auto_update_skips_when_current(tmp_path, monkeypatch):
    import mcpbrain.update as upd
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.3.0")
    monkeypatch.setattr(upd, "_latest_version", lambda url: "0.3.0")
    monkeypatch.setattr(upd, "update_from_index", lambda url: (_ for _ in ()).throw(AssertionError("must not update")))
    d = _daemon(tmp_path, auto_update_interval_s=3600.0)
    out = d.maybe_auto_update()
    assert out == {"updated": False}
