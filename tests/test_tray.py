"""Tests for TrayController (client-backed, GUI-free).

The controller wraps a ControlClient and never imports pystray. run_tray (the
pystray render layer) is manual-smoke only and not tested here.
"""

import sys

from mcpbrain.control_client import DaemonUnavailable
from mcpbrain.tray import TrayController


class FakeClient:
    """Stand-in for ControlClient. `up=False` simulates a stopped daemon."""

    def __init__(self, up=True, paused=False, count=7):
        self.up = up
        self.paused = paused
        self.count = count
        self.calls = []

    def status(self):
        self.calls.append("status")
        if not self.up:
            raise DaemonUnavailable("no daemon")
        return {"paused": self.paused, "chunk_count": self.count,
                "google_connected": True, "granted_scopes": [], "enrich_enabled": False}

    def pause(self):
        self.calls.append("pause")
        self.paused = True

    def resume(self):
        self.calls.append("resume")
        self.paused = False

    def wizard_url(self):
        return "http://127.0.0.1:50000/" if self.up else ""


def _ctrl(**kw):
    c = TrayController(FakeClient(**kw))
    c.refresh()
    return c


def test_status_text_running_shows_count():
    assert _ctrl(count=2249).status_text() == "2,249 items indexed"


def test_status_text_running_zero_count():
    assert _ctrl(count=0).status_text() == "Running"


def test_status_text_paused():
    assert _ctrl(paused=True).status_text() == "Paused"


def test_status_text_daemon_down():
    c = _ctrl(up=False)
    assert c.status_text() == "Daemon not running"
    assert c.is_paused() is False


def test_menu_toggle_and_enabled_flags():
    # Running: shows Pause, enabled.
    labels = [(l, e) for (l, _, e) in _ctrl().menu_items()]
    assert ("Pause", True) in labels
    # Paused: shows Resume.
    assert any(l == "Resume" and e for (l, _, e) in _ctrl(paused=True).menu_items())
    # Down: toggle present but disabled; status line + Quit still there.
    down = _ctrl(up=False).menu_items()
    assert down[0][2] is False                      # status line disabled
    assert any(l == "Quit" and e for (l, _, e) in down)
    assert any(l == "Pause" and e is False for (l, _, e) in down)


def test_pause_resume_call_client_and_refresh():
    c = TrayController(FakeClient())
    c.refresh()
    c.on_pause()
    assert c.is_paused() is True            # refreshed after pausing
    c.on_resume()
    assert c.is_paused() is False


def test_open_setup_opens_wizard_url(monkeypatch):
    import mcpbrain.tray as tray
    opened = {}
    monkeypatch.setattr(tray.webbrowser, "open", lambda u: opened.setdefault("url", u))
    _ctrl().on_open_setup()
    assert opened["url"] == "http://127.0.0.1:50000/"


def test_quit_sets_exit_without_stopping_daemon():
    c = _ctrl()
    assert c.should_exit() is False
    c.on_quit()
    assert c.should_exit() is True
    # on_quit must not touch the daemon (no pause/resume calls).
    assert "pause" not in c._client.calls and "resume" not in c._client.calls


def test_import_does_not_import_pystray():
    # Importing the tray module must not pull in the GUI backend.
    sys.modules.pop("pystray", None)
    import importlib
    import mcpbrain.tray
    importlib.reload(mcpbrain.tray)
    assert "pystray" not in sys.modules
