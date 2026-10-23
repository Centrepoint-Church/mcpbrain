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


def test_owner_repair_waits_while_the_model_loads(monkeypatch):
    """A model still building -> the owner repair waits for the next day
    rather than comparing with weaker lexical rules and moving the watermark."""
    from mcpbrain import action_owner
    called = []
    monkeypatch.setattr(action_owner, "repair_owner_actions",
                        lambda *a, **k: called.append(1))
    dm = _hygiene_daemon(_QuietStore())
    dm._embedder_factory = lambda: object()
    dm._embedder_bounded = lambda timeout=None: None
    out = dm._run_action_hygiene()
    assert called == [] and out["owner_repair"] == "embedder_loading"
    # M5: a skipped repair does not use up the day -- retried next tick.
    assert dm._last_action_hygiene is None


def test_owner_repair_embedder_build_error_skips_too(monkeypatch):
    from mcpbrain import action_owner
    called = []
    monkeypatch.setattr(action_owner, "repair_owner_actions",
                        lambda *a, **k: called.append(1))
    dm = _hygiene_daemon(_QuietStore())
    dm._embedder_factory = lambda: object()

    def boom(timeout=None):
        raise RuntimeError("model build failed")
    dm._embedder_bounded = boom
    out = dm._run_action_hygiene()
    assert called == [] and out["owner_repair"] == "embedder_failed"
    assert dm._last_action_hygiene is None


def test_owner_repair_reporting_a_skip_keeps_the_tick_due(monkeypatch):
    from mcpbrain import action_owner
    monkeypatch.setattr(action_owner, "repair_owner_actions",
                        lambda *a, **k: {"canonicalised": 2, "deduped": 0,
                                         "threads": 0, "skipped": "embedder_failed"})
    dm = _hygiene_daemon(_QuietStore())
    out = dm._run_action_hygiene()
    assert out["owner_repair"] == "embedder_failed"
    assert dm._last_action_hygiene is None
