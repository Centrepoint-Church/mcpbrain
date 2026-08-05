"""doctor must say when a live MCP server is running superseded code."""
from mcpbrain.doctor import version_drift_line


def _recs(*versions):
    return [{"pid": 1000 + i, "version": v, "started": 0}
            for i, v in enumerate(versions)]


def test_silent_when_every_server_matches(monkeypatch):
    monkeypatch.setattr("mcpbrain.doctor.live_version_records",
                        lambda home: _recs("0.7.113", "0.7.113"))
    assert version_drift_line("/tmp/h", installed="0.7.113") is None


def test_warns_when_one_server_is_stale(monkeypatch):
    monkeypatch.setattr("mcpbrain.doctor.live_version_records",
                        lambda home: _recs("0.7.112", "0.7.113"))
    line = version_drift_line("/tmp/h", installed="0.7.113")
    assert line is not None
    assert "0.7.112" in line and "0.7.113" in line
    assert "restart" in line.lower(), "must tell the user what to do"


def test_silent_when_no_servers_are_running(monkeypatch):
    """No MCP server is not a drift problem — doctor already covers connectivity."""
    monkeypatch.setattr("mcpbrain.doctor.live_version_records", lambda home: [])
    assert version_drift_line("/tmp/h", installed="0.7.113") is None


def test_counts_stale_servers_rather_than_naming_pids(monkeypatch):
    monkeypatch.setattr("mcpbrain.doctor.live_version_records",
                        lambda home: _recs("0.7.111", "0.7.112", "0.7.113"))
    line = version_drift_line("/tmp/h", installed="0.7.113")
    assert "2" in line, "should say how many are stale"
