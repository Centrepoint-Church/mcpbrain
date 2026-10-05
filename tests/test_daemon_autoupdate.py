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


def test_auto_update_off_by_default(tmp_path, monkeypatch):
    # Write an EMPTY config.json so is_configured() returns False.
    # OFF when unconfigured; daily-default when configured.
    import json
    (tmp_path / "config.json").write_text(json.dumps({}))
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d = _daemon(tmp_path)
    assert d.maybe_auto_update() is None


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


# ---------------------------------------------------------------------------
# the auto-update cadence must count SLEEP, not just awake time
#
# Same defect as the backup cadence (0.7.136): on macOS time.monotonic() does
# not advance while the machine sleeps, so a monotonic-only "daily" update
# check needed 24h of AWAKE time. Elapsed is now max(monotonic, wall-clock).
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, t): self.t = t
    def __call__(self): return self.t
    def advance(self, s): self.t += s


def _behind(monkeypatch):
    import mcpbrain.update as upd
    calls = []
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")

    def _latest(url):
        calls.append(url)
        return "0.3.0"
    monkeypatch.setattr(upd, "_latest_version", _latest)
    return calls


def test_auto_update_due_after_sleep_when_monotonic_clock_is_frozen(tmp_path, monkeypatch):
    calls = _behind(monkeypatch)
    mono, wall = _Clock(1000.0), _Clock(1_790_000_000.0)
    d = _daemon(tmp_path, auto_update_interval_s=86400.0, clock=mono, wall_clock=wall)
    assert d.maybe_auto_update()["update_available"] is True
    assert d.maybe_auto_update() is None  # just checked
    assert len(calls) == 1

    wall.advance(25 * 3600)  # asleep: monotonic frozen
    out = d.maybe_auto_update()
    assert out is not None and out["update_available"] is True, (
        "a slept day did not count toward the daily auto-update cadence")
    assert len(calls) == 2


def test_backwards_wall_clock_jump_does_not_suppress_a_due_auto_update(tmp_path, monkeypatch):
    calls = _behind(monkeypatch)
    mono, wall = _Clock(1000.0), _Clock(1_790_000_000.0)
    d = _daemon(tmp_path, auto_update_interval_s=86400.0, clock=mono, wall_clock=wall)
    d.maybe_auto_update()
    mono.advance(25 * 3600)
    wall.advance(25 * 3600 - 2 * 86400)  # clock set back two days meanwhile
    out = d.maybe_auto_update()
    assert out is not None and out["update_available"] is True
    assert len(calls) == 2


def test_failed_auto_update_check_still_backs_off_on_both_clocks(tmp_path, monkeypatch):
    """Failure semantics unchanged: a raising check stamps the cadence first,
    so it is not retried until a full interval passes on either clock."""
    import mcpbrain.update as upd
    calls = []
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")

    def _boom(url):
        calls.append(url)
        raise OSError("network down")
    monkeypatch.setattr(upd, "_latest_version", _boom)
    mono, wall = _Clock(1000.0), _Clock(1_790_000_000.0)
    d = _daemon(tmp_path, auto_update_interval_s=86400.0, clock=mono, wall_clock=wall)
    assert d.maybe_auto_update() is None
    mono.advance(3600)
    wall.advance(3600)
    assert d.maybe_auto_update() is None
    assert len(calls) == 1, "a failed check was retried early"
    wall.advance(24 * 3600)  # asleep past the interval
    d.maybe_auto_update()
    assert len(calls) == 2


def test_auto_update_without_wall_twin_falls_back_to_monotonic(monkeypatch):
    """A Daemon built via __new__ (or any path that sets _last_auto_update
    alone) has no _last_auto_update_wall; decide on monotonic only."""
    import threading
    calls = _behind(monkeypatch)
    d = Daemon.__new__(Daemon)
    d._config_lock = threading.Lock()
    d._auto_update_interval_s = 3600.0
    clock = _Clock(10_000.0)
    d._clock = clock
    d._last_auto_update = clock() - 60
    assert not hasattr(d, "_last_auto_update_wall")
    assert d.maybe_auto_update() is None  # not due, and no AttributeError
    assert calls == []
