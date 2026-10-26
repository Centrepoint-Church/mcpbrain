"""The action-hygiene cadence: keeps the actions table clean on a daily pass.

Before this, `archive_stale_actions` had NO automatic caller at all — it only ran
if someone manually invoked bin/consolidate.py, so on a real install the actions
table was never swept and accumulated years of debris.
"""
from mcpbrain import daemon as d


def test_action_hygiene_cadence_registered():
    assert "action_hygiene" in {cp.name for cp in d._CADENCE_PASSES}


def test_action_hygiene_default_and_key_present():
    assert d._CADENCE_DEFAULTS["action_hygiene_interval_s"] == 86400.0
    assert "action_hygiene_interval_s" in d._CADENCE_KEYS


def test_cadences_from_config_includes_action_hygiene(tmp_path):
    assert d._cadences_from_config(str(tmp_path))["action_hygiene_interval_s"] == 86400.0


def test_run_action_hygiene_exists():
    assert hasattr(d.Daemon, "_run_action_hygiene")


def test_run_action_hygiene_sweeps_and_reports(monkeypatch):
    """Runs both sweeps and the owner repair, and returns their combined counts."""
    calls = {}
    from mcpbrain import action_owner
    monkeypatch.setattr(
        action_owner, "repair_owner_actions",
        lambda store, owner, **kw: calls.setdefault("owner", kw)
        and {"canonicalised": 4, "deduped": 1, "threads": 2})

    class _Store:
        def archive_stale_actions(self, **kw):
            calls["stale"] = kw
            return {"archived": 3}

        def archive_duplicate_actions(self, **kw):
            calls["dupes"] = kw
            return {"archived": 2}

    dm = d.Daemon.__new__(d.Daemon)
    dm._store = _Store()
    dm._action_hygiene_interval_s = 86400.0
    dm._last_action_hygiene = None
    dm._clock = lambda: 1000.0

    out = dm._run_action_hygiene()
    assert out == {"actions_archived": 3, "actions_deduped": 2,
                   "owner_canonicalised": 4, "owner_deduped": 1}
    assert "stale" in calls and "dupes" in calls and "owner" in calls
    assert calls["owner"]["embedder"] is None   # no model configured: lexical
    assert dm._last_action_hygiene == 1000.0


def test_run_action_hygiene_skips_when_not_due():
    dm = d.Daemon.__new__(d.Daemon)
    dm._action_hygiene_interval_s = None      # OFF -> never due
    dm._last_action_hygiene = None
    dm._clock = lambda: 1000.0
    assert dm._run_action_hygiene() is None


def test_run_action_hygiene_survives_store_failure():
    """A sweep blowing up must not take the daemon cycle down with it."""
    class _Boom:
        def archive_stale_actions(self, **kw):
            raise RuntimeError("db locked")

        def archive_duplicate_actions(self, **kw):
            return {"archived": 0}

    dm = d.Daemon.__new__(d.Daemon)
    dm._store = _Boom()
    dm._action_hygiene_interval_s = 86400.0
    dm._last_action_hygiene = None
    dm._clock = lambda: 1000.0

    out = dm._run_action_hygiene()
    assert out is not None and out.get("action_hygiene") is False
    assert "db locked" in out.get("error", "")


def _hygiene_daemon(store):
    dm = d.Daemon.__new__(d.Daemon)
    dm._store = store
    dm._action_hygiene_interval_s = 86400.0
    dm._last_action_hygiene = None
    dm._clock = lambda: 1000.0
    return dm


class _QuietStore:
    def archive_stale_actions(self, **kw):
        return {"archived": 0}

    def archive_duplicate_actions(self, **kw):
        return {"archived": 0}


def test_owner_repair_failure_does_not_lose_the_sweeps(monkeypatch):
    from mcpbrain import action_owner

    def boom(*a, **k):
        raise RuntimeError("owner repair broke")
    monkeypatch.setattr(action_owner, "repair_owner_actions", boom)
    out = _hygiene_daemon(_QuietStore())._run_action_hygiene()
    assert out["actions_archived"] == 0 and out["owner_repair_error"]


def test_owner_repair_uses_the_built_embedder(monkeypatch):
    from mcpbrain import action_owner
    seen = {}
    monkeypatch.setattr(action_owner, "repair_owner_actions",
                        lambda store, owner, **kw: seen.update(kw) or
                        {"canonicalised": 0, "deduped": 0, "threads": 0})
    dm = _hygiene_daemon(_QuietStore())
    emb = object()
    dm._embedder_factory = lambda: emb
    dm._embedder_bounded = lambda timeout=None: emb
    dm._run_action_hygiene()
    assert seen["embedder"] is emb


def _capture(monkeypatch):
    from mcpbrain import action_owner
    calls = []

    def fake(store, owner, **kw):
        calls.append(kw)
        return {"canonicalised": 1, "deduped": 0, "threads": 0,
                "skipped": kw.get("skip_comparison")}
    monkeypatch.setattr(action_owner, "repair_owner_actions", fake)
    return calls


def test_owner_repair_canonicalises_while_the_model_loads(monkeypatch):
    """M-N3: a model still building -> canonicalise still runs, only the
    comparison is skipped, and the cadence stays due."""
    calls = _capture(monkeypatch)
    dm = _hygiene_daemon(_QuietStore())
    dm._embedder_factory = lambda: object()
    dm._embedder_bounded = lambda timeout=None: None
    out = dm._run_action_hygiene()
    assert len(calls) == 1 and calls[0]["skip_comparison"] == "embedder_loading"
    assert calls[0]["embedder"] is None
    assert out["owner_repair"] == "embedder_loading" and out["owner_canonicalised"] == 1
    assert dm._last_action_hygiene is None


def test_owner_repair_embedder_build_error_still_canonicalises(monkeypatch):
    calls = _capture(monkeypatch)
    dm = _hygiene_daemon(_QuietStore())
    dm._embedder_factory = lambda: object()

    def boom(timeout=None):
        raise RuntimeError("model build failed")
    dm._embedder_bounded = boom
    out = dm._run_action_hygiene()
    assert calls[0]["skip_comparison"] == "embedder_failed"
    assert out["owner_repair"] == "embedder_failed"
    assert dm._last_action_hygiene is None


def test_owner_repair_backs_off_a_failing_factory(monkeypatch, caplog):
    """M-N1: a factory raising on every call is called once per backoff step
    (60 s doubling to a 3600 s ceiling), one WARNING per call, never per tick;
    a success resets the backoff."""
    import logging
    calls = _capture(monkeypatch)
    dm = _hygiene_daemon(_QuietStore())
    clock = {"t": 1000.0}
    dm._clock = lambda: clock["t"]
    built = []
    state = {"fail": True}

    def bounded(timeout=None):
        built.append(clock["t"])
        if state["fail"]:
            raise RuntimeError("no model")
        return object()
    dm._embedder_factory = lambda: None
    dm._embedder_bounded = bounded
    caplog.set_level(logging.WARNING, logger="mcpbrain.daemon")

    def tick(at):
        clock["t"] = at
        return dm._run_owner_action_repair()

    tick(1000.0)
    for t in (1010.0, 1030.0, 1059.0):               # inside the 60 s backoff
        assert tick(t)["owner_repair"] == "embedder_failed"
    assert built == [1000.0]
    assert len([r for r in caplog.records if "embedder failed" in r.message]) == 1
    tick(1060.0)                                     # next step: 120 s
    assert built == [1000.0, 1060.0]
    tick(1179.0)
    assert built == [1000.0, 1060.0]
    tick(1180.0)                                     # 240 s
    assert dm._owner_repair_backoff_s == 240.0
    for _ in range(12):                              # doubles to the ceiling
        tick(clock["t"] + dm._owner_repair_backoff_s)
    assert dm._owner_repair_backoff_s == 3600.0
    assert len([r for r in caplog.records if "embedder failed" in r.message]) == len(built)
    assert all(c["skip_comparison"] == "embedder_failed" for c in calls)
    state["fail"] = False                            # success resets
    out = tick(clock["t"] + 3600.0)
    assert "owner_repair" not in out and dm._owner_repair_backoff_s is None
    state["fail"] = True
    tick(clock["t"] + 1.0)
    assert dm._owner_repair_backoff_s == 60.0


def test_owner_repair_reporting_a_skip_keeps_the_tick_due(monkeypatch):
    from mcpbrain import action_owner
    monkeypatch.setattr(action_owner, "repair_owner_actions",
                        lambda *a, **k: {"canonicalised": 2, "deduped": 0,
                                         "threads": 0, "skipped": "embedder_failed"})
    dm = _hygiene_daemon(_QuietStore())
    out = dm._run_action_hygiene()
    assert out["owner_repair"] == "embedder_failed"
    assert dm._last_action_hygiene is None
