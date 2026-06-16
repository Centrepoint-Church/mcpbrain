"""`mcpbrain fleet-report` dispatch + behaviour."""
import pytest

from mcpbrain import cli


def test_fleet_report_not_configured_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.folder_id
    with pytest.raises(SystemExit) as ei:
        cli.main(["fleet-report"])
    assert ei.value.code == 1
    assert "fleet.folder_id not set" in capsys.readouterr().out


def test_fleet_report_writes_report_and_prints_url(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, fleet
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    monkeypatch.setattr("mcpbrain.fleet_cli._build_drive_service", lambda: "SVC")
    monkeypatch.setattr(fleet, "write_report", lambda home, svc: None)
    cli.main(["fleet-report"])
    out = capsys.readouterr().out
    assert "FLEET1" in out and "drive.google.com" in out


def test_fleet_report_beacon_flag_calls_write_beacon(tmp_path, monkeypatch):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, fleet
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEET1"}})
    monkeypatch.setattr("mcpbrain.fleet_cli._build_drive_service", lambda: "SVC")
    called = {}
    monkeypatch.setattr(fleet, "write_beacon",
                        lambda home, svc: called.setdefault("args", (home, svc)))
    cli.main(["fleet-report", "--beacon"])
    assert called["args"][1] == "SVC"
