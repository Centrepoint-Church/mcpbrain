"""Cross-platform cadence generators call `mcpbrain <subcommand>`, not shell scripts."""
import pytest

from mcpbrain import agents


def test_launchd_prune_calls_subcommand_not_shell(tmp_path):
    plist = agents.records_prune_plist(
        mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home="/h")
    assert "records-prune" in plist
    assert "/bin/sh" not in plist and "prune_hot_md.py" not in plist


def test_schtasks_prune_daily():
    a = agents.prune_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert "/sc" in a and "daily" in a and "/st" in a and "06:00" in a
    assert any("records-prune" in x for x in a)


def test_schtasks_health_weekly_monday():
    a = agents.health_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert "weekly" in a and "MON" in a and any("records-health" in x for x in a)


def test_install_cadences_dispatches_by_platform(monkeypatch):
    calls = []
    monkeypatch.setattr(agents, "_install_cadences_launchd", lambda **k: calls.append("darwin"))
    monkeypatch.setattr(agents, "_install_cadences_schtasks", lambda **k: calls.append("win32"))
    agents.install_cadences("darwin", mcpbrain_bin="/x", home="/h")
    assert calls == ["darwin"]
    agents.install_cadences("win32", mcpbrain_bin="/x", home="/h")
    assert calls[-1] == "win32"
    with pytest.raises(ValueError):
        agents.install_cadences("plan9", mcpbrain_bin="/x", home="/h")


def test_gardener_and_meeting_packs_generators_removed():
    import mcpbrain.agents as agents
    for n in ("records_gardener_plist","meeting_packs_plist","gardener_schtasks_args","meeting_packs_schtasks_args"):
        assert not hasattr(agents, n)


def test_launchd_beacon_calls_subcommand_hourly(tmp_path):
    plist = agents.fleet_beacon_plist(
        mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home="/h")
    assert "fleet-report" in plist and "--beacon" in plist
    assert "/bin/sh" not in plist
    # hourly via StartInterval (3600s) — not a calendar time
    assert "<integer>3600</integer>" in plist
    assert "StartInterval" in plist


def test_schtasks_beacon_hourly():
    a = agents.fleet_beacon_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert "/sc" in a and "hourly" in a
    assert any("fleet-report" in x and "--beacon" in x for x in a)


def test_cadence_specs_include_beacon_only_when_fleet_configured():
    # _cadence_specs is the pure (label, thunk) builder the OS installers iterate;
    # it gates the beacon on fleet config without invoking launchctl/schtasks.
    specs_on = agents._cadence_specs(home_fleet_configured=True,
                                     mcpbrain_bin="/x", home="/h")
    labels_on = [label for label, _ in specs_on]
    assert agents._FLEET_BEACON_LABEL in labels_on
    # the beacon thunk renders a valid plist
    beacon_thunk = dict(specs_on)[agents._FLEET_BEACON_LABEL]
    assert "fleet-report" in beacon_thunk()

    specs_off = agents._cadence_specs(home_fleet_configured=False,
                                      mcpbrain_bin="/x", home="/h")
    assert agents._FLEET_BEACON_LABEL not in [label for label, _ in specs_off]
