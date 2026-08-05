"""Per-process version records, because there are multiple live MCP servers."""
import json
import os

from mcpbrain.mcp_server import live_version_records, write_version_record


def test_record_carries_pid_and_version(tmp_path):
    write_version_record(str(tmp_path))
    recs = live_version_records(str(tmp_path))
    assert len(recs) == 1
    assert recs[0]["pid"] == os.getpid()
    from mcpbrain import __version__
    assert recs[0]["version"] == __version__


def test_dead_pids_are_pruned(tmp_path):
    d = tmp_path / "mcp_heartbeat"
    d.mkdir(parents=True)
    (d / "999999.json").write_text(json.dumps(
        {"pid": 999999, "version": "0.0.1", "started": 0}), encoding="utf-8")
    write_version_record(str(tmp_path))
    pids = {r["pid"] for r in live_version_records(str(tmp_path))}
    assert 999999 not in pids
    assert not (d / "999999.json").exists(), "dead record should be removed"


def test_multiple_live_servers_are_all_reported(tmp_path):
    d = tmp_path / "mcp_heartbeat"
    d.mkdir(parents=True)
    write_version_record(str(tmp_path))
    # a second live record: reuse this process's pid under a different filename
    # is not valid, so use the parent pid, which is alive.
    (d / f"{os.getppid()}.json").write_text(json.dumps(
        {"pid": os.getppid(), "version": "0.0.1", "started": 0}), encoding="utf-8")
    assert len(live_version_records(str(tmp_path))) == 2


def test_write_never_raises_into_startup(tmp_path, monkeypatch):
    """Same best-effort contract as write_heartbeat: never break a connect."""
    monkeypatch.setattr("mcpbrain.mcp_server.Path", lambda *a, **k: (_ for _ in ()).throw(OSError))
    write_version_record(str(tmp_path))   # must not raise
