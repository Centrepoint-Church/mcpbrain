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
    for n in ("records_gardener_plist", "meeting_packs_plist", "gardener_schtasks_args", "meeting_packs_schtasks_args"):
        assert not hasattr(agents, n)
