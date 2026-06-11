"""The daemon's maybe_enrich cadence kicks the headless-Claude-Code backfill loop."""
import mcpbrain.config as config
from mcpbrain import daemon as daemon_mod


def _daemon(monkeypatch, tmp_path, *, configured=True, paused=False, interval=1800.0):
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    monkeypatch.setattr(config, "is_configured", lambda home: configured)
    d = daemon_mod.Daemon.__new__(daemon_mod.Daemon)
    d._enrich_interval_s = interval
    d._last_enrich = None
    d._clock = lambda: 1000.0
    d.is_paused = lambda: paused
    d._kicks = []
    d.start_enrich_backfill = lambda: d._kicks.append(1)
    return d


def test_kicks_when_due_and_configured(tmp_path, monkeypatch):
    d = _daemon(monkeypatch, tmp_path)
    d.maybe_enrich()
    assert d._kicks == [1]
    assert (tmp_path / "logs" / "enrich.log").exists()       # durable heartbeat written
    d.maybe_enrich()                                          # not due again (same clock)
    assert d._kicks == [1]


def test_skips_when_not_configured(tmp_path, monkeypatch):
    d = _daemon(monkeypatch, tmp_path, configured=False)
    d.maybe_enrich()
    assert d._kicks == [] and not (tmp_path / "logs" / "enrich.log").exists()


def test_skips_when_paused(tmp_path, monkeypatch):
    d = _daemon(monkeypatch, tmp_path, paused=True)
    d.maybe_enrich()
    assert d._kicks == []


def test_off_when_interval_none(tmp_path, monkeypatch):
    d = _daemon(monkeypatch, tmp_path, interval=None)
    d.maybe_enrich()
    assert d._kicks == []


def test_enrich_interval_defaults_on(tmp_path, monkeypatch):
    # absent cadence key -> default 1800 (ON), not None (OFF)
    (tmp_path / "config.json").write_text('{"cadences": {}}')
    cad = daemon_mod._cadences_from_config(str(tmp_path))
    assert cad["enrich_interval_s"] == 1800.0
