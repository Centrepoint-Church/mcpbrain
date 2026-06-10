"""Cross-platform cowork cadence generators call `mcpbrain <subcommand>`, not shell scripts."""
from mcpbrain import agents


def test_launchd_gardener_calls_subcommand():
    plist = agents.records_gardener_plist(mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home="/h")
    assert "records-gardener" in plist
    assert "/bin/bash" not in plist and "run_memory_gardener.sh" not in plist
    assert "RunAtLoad" not in plist  # weekly-only, expensive


def test_launchd_meeting_packs_twice_daily_subcommand():
    plist = agents.meeting_packs_plist(home="/Users/x/.mcpbrain", mcpbrain_bin="/usr/local/bin/mcpbrain")
    assert "meeting-packs" in plist and "build_meeting_packs.sh" not in plist
    assert "<integer>45</integer>" in plist and "<integer>12</integer>" in plist


def test_systemd_gardener_timer_weekly_monday_0800():
    service, timer = agents.gardener_timer_units(mcpbrain_bin="/m", home="/h")
    assert "records-gardener" in service and "OnCalendar=Mon *-*-* 08:00" in timer


def test_systemd_meeting_packs_timer_has_two_times():
    service, timer = agents.meeting_packs_timer_units(mcpbrain_bin="/m", home="/h")
    assert "meeting-packs" in service
    assert "07:45" in timer and "12:00" in timer


def test_schtasks_gardener_weekly():
    a = agents.gardener_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert "weekly" in a and "MON" in a and any("records-gardener" in x for x in a)


def test_schtasks_meeting_packs_returns_two_tasks():
    tasks = agents.meeting_packs_schtasks_args(mcpbrain_bin=r"C:\mcpbrain.exe")
    assert len(tasks) == 2
    assert any("07:45" in " ".join(t) for t in tasks)
    assert any("12:00" in " ".join(t) for t in tasks)
