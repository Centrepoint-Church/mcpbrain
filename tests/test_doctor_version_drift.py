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


def test_remedy_names_every_client_kind_not_just_desktop(monkeypatch):
    """The remedy said "restart Claude Desktop", which is incomplete and was
    actively misleading in practice: a live server belongs to whichever client
    spawned it, and on 2026-09-09 two of five stale servers on the author's box
    were long-running Claude Code CLI sessions (7 and 6 days old) that no
    Desktop restart can touch. Restarting Desktop and re-running doctor left
    the warning standing, with nothing explaining why.

    Stay platform-neutral: "quit completely" rather than Cmd-Q, since the same
    line ships to Windows.
    """
    monkeypatch.setattr("mcpbrain.doctor.live_version_records",
                        lambda home: _recs("0.7.112", "0.7.113"))
    line = version_drift_line("/tmp/h", installed="0.7.113")
    lower = line.lower()
    assert "claude desktop" in lower, "must still name Desktop"
    assert "claude code" in lower, "must also name Claude Code sessions"
    assert "cmd-q" not in lower and "⌘" not in line, "keep it platform-neutral"
