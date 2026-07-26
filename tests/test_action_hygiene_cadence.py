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
    """Runs both sweeps and returns their combined counts."""
    calls = {}

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
    assert out == {"actions_archived": 3, "actions_deduped": 2}
    assert "stale" in calls and "dupes" in calls
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
