"""The mcp_heartbeat sweep cadence: prunes dead-pid mcp_heartbeat/<pid>.json files.

Before this, live_version_records(home) -- the only thing that prunes these files --
was called ONLY from doctor.py's version_drift_line, and run_doctor has no caller
anywhere else in the codebase. So a dead MCP server's heartbeat file only got
cleaned up if a user manually ran `mcpbrain doctor` from a terminal. This is the
same unbounded-growth shape as the ~24GB of orphaned mcpbrain-snap-* work dirs
found live on 2026-07-27 (see backup.sweep_orphan_snapshots) -- tiny files this
time, but the same missing periodic sweep.
"""
from mcpbrain import daemon as d


def test_mcp_heartbeat_sweep_cadence_registered():
    assert "mcp_heartbeat_sweep" in {cp.name for cp in d._CADENCE_PASSES}


def test_mcp_heartbeat_sweep_default_and_key_present():
    assert d._CADENCE_DEFAULTS["mcp_heartbeat_sweep_interval_s"] == 3600.0
    assert "mcp_heartbeat_sweep_interval_s" in d._CADENCE_KEYS


def test_cadences_from_config_includes_mcp_heartbeat_sweep(tmp_path):
    assert d._cadences_from_config(str(tmp_path))["mcp_heartbeat_sweep_interval_s"] == 3600.0


def test_run_mcp_heartbeat_sweep_exists():
    assert hasattr(d.Daemon, "_run_mcp_heartbeat_sweep")


def test_run_mcp_heartbeat_sweep_is_identity_agnostic():
    """Sweeping our own heartbeat files needs no Google identity."""
    cp = next(cp for cp in d._CADENCE_PASSES if cp.name == "mcp_heartbeat_sweep")
    assert cp.needs_configured is False
    assert cp.needs_bulk_lock is False


def test_run_mcp_heartbeat_sweep_prunes_and_reports(tmp_path, monkeypatch):
    """Runs live_version_records(home) and reports how many records remain live."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    d_dir = tmp_path / "mcp_heartbeat"
    d_dir.mkdir()
    (d_dir / "999999.json").write_text(
        '{"pid": 999999, "version": "0.0.1", "started": 0}', encoding="utf-8")

    dm = d.Daemon.__new__(d.Daemon)
    dm._mcp_heartbeat_sweep_interval_s = 3600.0
    dm._last_mcp_heartbeat_sweep = None
    dm._clock = lambda: 1000.0

    out = dm._run_mcp_heartbeat_sweep()
    assert out == {"mcp_heartbeat_live": 0}
    assert not (d_dir / "999999.json").exists(), "dead record should have been pruned"
    assert dm._last_mcp_heartbeat_sweep == 1000.0


def test_run_mcp_heartbeat_sweep_skips_when_not_due():
    dm = d.Daemon.__new__(d.Daemon)
    dm._mcp_heartbeat_sweep_interval_s = None      # OFF -> never due
    dm._last_mcp_heartbeat_sweep = None
    dm._clock = lambda: 1000.0
    assert dm._run_mcp_heartbeat_sweep() is None


def test_run_mcp_heartbeat_sweep_survives_failure(monkeypatch):
    """A sweep blowing up must not take the daemon cycle down with it."""
    from mcpbrain import mcp_server

    def _boom(home):
        raise OSError("permission denied")

    monkeypatch.setattr(mcp_server, "live_version_records", _boom)

    dm = d.Daemon.__new__(d.Daemon)
    dm._mcp_heartbeat_sweep_interval_s = 3600.0
    dm._last_mcp_heartbeat_sweep = None
    dm._clock = lambda: 1000.0

    out = dm._run_mcp_heartbeat_sweep()
    assert out is not None and out.get("mcp_heartbeat_sweep") is False
    assert "permission denied" in out.get("error", "")
