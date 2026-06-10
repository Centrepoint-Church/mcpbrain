import json
import mcpbrain.update as upd
from mcpbrain.store import Store
from mcpbrain.daemon import Daemon, SingleWriterLock


class _Emb:
    dim = 4
    def embed_passages(self, t): return [[0.0]*4 for _ in t]


def _daemon(tmp_path, monkeypatch, **kw):
    (tmp_path/"config.json").write_text(json.dumps(
        {"owner_name":"S","owner_email":"s@x","orgs":[{"name":"O"}]}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = Store(tmp_path/"b.sqlite3", dim=4, read_only=False); s.init()
    return Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path/"d.lock"),
                  clock=lambda: 1e9, **kw)


def test_auto_update_default_on_when_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upd, "_latest_version", lambda u: "0.3.0")
    d = _daemon(tmp_path, monkeypatch)  # no explicit auto_update_interval_s
    out = d.maybe_auto_update()
    assert out and out.get("update_available") is True and d._pending_update == "0.3.0"


def test_auto_update_detect_does_not_install(tmp_path, monkeypatch):
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upd, "_latest_version", lambda u: "0.3.0")
    monkeypatch.setattr(upd, "update_from_index", lambda u: (_ for _ in ()).throw(AssertionError("must not install in-loop")))
    d = _daemon(tmp_path, monkeypatch)
    d.maybe_auto_update()  # only detects; install happens in run() after lock release


def test_auto_update_off_when_unconfigured(tmp_path, monkeypatch):
    (tmp_path/"config.json").write_text("{}")
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    s = Store(tmp_path/"b.sqlite3", dim=4, read_only=False); s.init()
    d = Daemon(s, _Emb(), services={}, lock=SingleWriterLock(tmp_path/"d2.lock"), clock=lambda: 1e9)
    assert d.maybe_auto_update() is None


def test_auto_update_skips_change_me_url(tmp_path, monkeypatch):
    monkeypatch.setattr(upd, "_index_url", lambda: "https://CHANGE-ME.github.io/mcpbrain-dist/simple/")
    d = _daemon(tmp_path, monkeypatch)
    assert d.maybe_auto_update() is None and d._pending_update is None
