import json, time, os
from datetime import datetime, timedelta, timezone
from mcpbrain.monitor import run_monitor


def _home(tmp_path, cfg=None):
    (tmp_path / "config.json").write_text(json.dumps(cfg or {})); return str(tmp_path)

def _hb(tmp_path, days=0):
    ts = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    (tmp_path / "mcp_heartbeat.json").write_text(json.dumps({"last_seen": ts}))

def _enrich(tmp_path, days=0):
    logs = tmp_path / "logs"; logs.mkdir(exist_ok=True)
    p = logs / "enrich.log"; p.write_text("[ts] drained\n")
    if days: os.utime(str(p), (time.time()-days*86400,)*2)

def test_healthy_returns_ok_zero(tmp_path):
    home = _home(tmp_path); _hb(tmp_path); _enrich(tmp_path)
    code, msg = run_monitor(home)
    assert code == 0 and "ok" in msg.lower()

def test_daemon_down_exits_1(tmp_path):
    home = _home(tmp_path); _enrich(tmp_path)
    code, _ = run_monitor(home); assert code == 1

def test_enrichment_idle_exits_1(tmp_path):
    home = _home(tmp_path); _hb(tmp_path)
    code, msg = run_monitor(home); assert code == 1 and "enrich" in msg.lower()

def test_sync_error_exits_1(tmp_path):
    home = _home(tmp_path); _hb(tmp_path); _enrich(tmp_path)
    logs = tmp_path / "logs"; (logs / "error.log").write_text("sync failed\n")
    code, msg = run_monitor(home); assert code == 1

def test_cli_monitor_registered(tmp_path, monkeypatch):
    import mcpbrain.monitor as mon
    called = {}
    monkeypatch.setattr(mon, "run_monitor", lambda home: (called.setdefault("home", home), (0, "ok"))[1])
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path)); (tmp_path / "config.json").write_text("{}")
    import pytest
    from mcpbrain import cli
    with pytest.raises(SystemExit):
        cli.main(["monitor"])
    assert "home" in called
