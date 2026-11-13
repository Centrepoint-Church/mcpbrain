"""`mcpbrain fleet-report` dispatch + behaviour."""
import json

import pytest

from mcpbrain import cli


@pytest.fixture(autouse=True)
def _no_tenant_fleet_folder(tmp_path, monkeypatch):
    """Tests here control the fleet folder through config; the build's own
    tenant profile must not supply one behind their back."""
    from mcpbrain import tenant
    prof = tmp_path / "tenant.json"
    prof.write_text(json.dumps({"tenant_id": "t", "display_name": "T",
                                "oauth_project_id": "p", "marketplace_owner": "o",
                                "marketplace_repo": "r", "marketplace_name": "n",
                                "fleet_folder_id": ""}))
    monkeypatch.setenv("MCPBRAIN_TENANT", str(prof))
    tenant._clear_cache()
    yield
    tenant._clear_cache()


def test_fleet_report_uses_the_tenant_fleet_folder(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config, fleet, tenant
    config.write_config(str(tmp_path), {})
    (tmp_path / "tenant.json").write_text(json.dumps({
        "tenant_id": "t", "display_name": "T", "oauth_project_id": "p",
        "marketplace_owner": "o", "marketplace_repo": "r", "marketplace_name": "n",
        "fleet_folder_id": "TENANT_FLEET"}))
    tenant._clear_cache()
    monkeypatch.setattr("mcpbrain.fleet_cli._build_drive_service", lambda: "SVC")
    monkeypatch.setattr(fleet, "write_report", lambda home, svc: None)
    cli.main(["fleet-report"])
    assert "TENANT_FLEET" in capsys.readouterr().out


def test_fleet_report_not_configured_exits_1(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.folder_id
    with pytest.raises(SystemExit) as ei:
        cli.main(["fleet-report"])
    assert ei.value.code == 1
    assert "no fleet folder" in capsys.readouterr().out


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


def test_fleet_report_beacon_unconfigured_exits_clean_no_error(tmp_path, monkeypatch):
    """An orphaned hourly --beacon cadence on an unconfigured/cleared fleet must
    no-op cleanly (exit 0, no Drive build, no error spam) — not exit 1 hourly."""
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    from mcpbrain import config
    config.write_config(str(tmp_path), {})  # no fleet.folder_id
    built = {"n": 0}
    monkeypatch.setattr("mcpbrain.fleet_cli._build_drive_service",
                        lambda: built.update(n=built["n"] + 1))
    # Returns normally (no SystemExit), and never tries to build a Drive service.
    cli.main(["fleet-report", "--beacon"])
    assert built["n"] == 0
