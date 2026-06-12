import json
import threading
import time
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


def test_periodic_passes_skip_while_backfill_active(tmp_path, monkeypatch):
    d = _daemon(tmp_path, monkeypatch)
    # Wire communities so it would run when not blocked (interval=0 means always due).
    d._communities_interval_s = 0.0
    d._last_communities = None

    called = {"n": 0}
    # Patch _run_communities — the dispatch table calls _run_X directly.
    monkeypatch.setattr(d, "_run_communities", lambda: called.__setitem__("n", called["n"] + 1))

    # Backfill active — should early-return; communities NOT called.
    d._backfill_active.set()
    d._run_periodic_passes()
    assert called["n"] == 0

    # Backfill cleared — communities should now be called.
    d._backfill_active.clear()
    d._run_periodic_passes()
    assert called["n"] == 1


def test_maybe_resolve_and_backup_skip_while_backfill_active(tmp_path, monkeypatch):
    """Guards must fire BEFORE the real cadence checks, not rely on them."""
    d = _daemon(tmp_path, monkeypatch)
    # Force backup to be due so the ONLY thing stopping it is the backfill guard.
    import mcpbrain.backup as _backup_mod
    from mcpbrain.daemon import BackupConfig
    d._backup = BackupConfig(
        key=b"k" * 32,
        drive_service=object(),
        shared_drive_id="sid",
        user_id="uid",
        out_path=tmp_path / "snap.enc",
    )
    d._backup_interval_s = 1.0
    d._last_backup = None    # first call is always due

    inner_called = {"backup": 0}
    monkeypatch.setattr(
        _backup_mod, "make_encrypted_snapshot",
        lambda *a, **kw: inner_called.__setitem__("backup", inner_called["backup"] + 1) or (tmp_path / "snap.enc"),
    )

    # With backfill active: backup should return None without doing any work.
    d._backfill_active.set()
    assert d.maybe_backup() is None
    assert inner_called == {"backup": 0}
