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


def test_auto_update_detects_when_due_and_behind(tmp_path, monkeypatch):
    """maybe_auto_update now DETECTS only — no install inside the loop.

    It sets _pending_update and returns update_available=True; the actual
    uv install + restart happens in run() AFTER the write lock is released.
    update_from_index must NOT be called here.
    """
    import mcpbrain.update as upd
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upd, "_latest_version", lambda url: "0.3.0")
    monkeypatch.setattr(upd, "update_from_index",
                        lambda url: (_ for _ in ()).throw(AssertionError("must not install in-loop")))
    d = _daemon(tmp_path, auto_update_interval_s=3600.0)
    out = d.maybe_auto_update()  # first call: due (last is None)
    # Detect-only: update_available flag set, pending_update stashed, no install.
    assert out is not None and out.get("update_available") is True
    assert d._pending_update == "0.3.0"


def test_auto_update_skips_when_current(tmp_path, monkeypatch):
    import mcpbrain.update as upd
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.3.0")
    monkeypatch.setattr(upd, "_latest_version", lambda url: "0.3.0")
    monkeypatch.setattr(upd, "update_from_index", lambda url: (_ for _ in ()).throw(AssertionError("must not update")))
    d = _daemon(tmp_path, auto_update_interval_s=3600.0)
    out = d.maybe_auto_update()
    # No update available: returns None (not {"updated": False})
    assert out is None
    assert d._pending_update is None
