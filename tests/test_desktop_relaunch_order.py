# tests/test_desktop_relaunch_order.py
"""Claude Desktop rewrites its config on quit, so the connector write must land
AFTER the app has exited and BEFORE it is relaunched."""
from mcpbrain import control_api, desktop


def test_relaunch_runs_the_callback_between_quit_and_launch(monkeypatch):
    calls = []
    monkeypatch.setattr(desktop, "quit_claude_desktop",
                        lambda: calls.append("quit") or {"quit": True, "detail": ""})
    monkeypatch.setattr(desktop, "launch_claude_desktop",
                        lambda: calls.append("launch") or {"launched": True, "detail": ""})

    result = desktop.relaunch_claude_desktop(on_quit=lambda: calls.append("write"))

    assert calls == ["quit", "write", "launch"]
    assert result["relaunched"] is True


def test_callback_failure_still_relaunches(monkeypatch):
    # A failed connector write must never leave the user with Claude Desktop shut.
    calls = []
    monkeypatch.setattr(desktop, "quit_claude_desktop",
                        lambda: calls.append("quit") or {"quit": True, "detail": ""})
    monkeypatch.setattr(desktop, "launch_claude_desktop",
                        lambda: calls.append("launch") or {"launched": True, "detail": ""})

    def boom():
        calls.append("write")
        raise OSError("nope")

    result = desktop.relaunch_claude_desktop(on_quit=boom)
    assert calls == ["quit", "write", "launch"]
    assert result["relaunched"] is True


def test_relaunch_never_raises(monkeypatch):
    monkeypatch.setattr(desktop, "quit_claude_desktop",
                        lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    result = desktop.relaunch_claude_desktop(on_quit=lambda: None)
    assert result["relaunched"] is False and "restart" in result["detail"].lower()
