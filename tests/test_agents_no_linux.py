import pytest
import mcpbrain.agents as agents

def test_systemd_symbols_gone():
    for n in ("systemd_unit", "systemd_tray_unit", "prune_timer_units",
              "health_timer_units", "gardener_timer_units", "meeting_packs_timer_units"):
        assert not hasattr(agents, n), f"{n} should be deleted"

def test_dispatchers_reject_linux():
    for fn in (lambda: agents.install_agent("linux", mcpbrain_bin="/x", home="/h"),
               lambda: agents.uninstall_agent("linux"),
               lambda: agents.restart_agent("linux"),
               lambda: agents.install_cadences("linux", mcpbrain_bin="/x", home="/h")):
        with pytest.raises(ValueError, match="[Uu]nsupported"):
            fn()

def test_darwin_and_win32_still_accepted(monkeypatch):
    calls = []
    monkeypatch.setattr(agents, "_install_cadences_launchd", lambda **k: calls.append("darwin"))
    monkeypatch.setattr(agents, "_install_cadences_schtasks", lambda **k: calls.append("win32"))
    agents.install_cadences("darwin", mcpbrain_bin="/x", home="/h")
    agents.install_cadences("win32", mcpbrain_bin="/x", home="/h")
    assert calls == ["darwin", "win32"]
