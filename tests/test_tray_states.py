"""Tests for TrayController icon-state, attention derivation, and contextual menu."""
from mcpbrain.tray import TrayController


class FakeClient:
    def __init__(self, status): self._s = status
    def status(self): return self._s
    def pause(self): pass
    def resume(self): pass
    def wizard_url(self): return "http://127.0.0.1:1/"
    def dashboard_url(self): return "http://127.0.0.1:1/dashboard"
    def reconnect_google(self): return {}


def _ctrl(status):
    c = TrayController(FakeClient(status))
    c.refresh()
    return c


_OK = {"google": {"state": "ok", "detail": "", "last_verified": None}}


def test_icon_state_running():
    c = _ctrl({"paused": False, "chunk_count": 10, "connections": _OK})
    assert c.icon_state() == "running"


def test_icon_state_paused():
    c = _ctrl({"paused": True, "chunk_count": 10, "connections": _OK})
    assert c.icon_state() == "paused"


def test_icon_state_attention_when_connection_needs_action():
    conns = {"google": {"state": "needs_action", "detail": "Access expired — reconnect",
                        "last_verified": None}}
    c = _ctrl({"paused": False, "chunk_count": 10, "connections": conns})
    assert c.icon_state() == "attention"
    a = c.attention()
    assert a and a[0]["connection"] == "google"
    assert "reconnect" in a[0]["detail"].lower()


def test_icon_state_unavailable_when_daemon_down():
    from mcpbrain.control_client import DaemonUnavailable

    class DownClient:
        def status(self): raise DaemonUnavailable("down")
        def pause(self): pass
        def resume(self): pass
        def wizard_url(self): return ""
        def dashboard_url(self): return ""

    c = TrayController(DownClient())
    c.refresh()
    assert c.icon_state() == "unavailable"


def test_status_text_includes_attention():
    conns = {"google": {"state": "needs_action", "detail": "Access expired — reconnect",
                        "last_verified": None}}
    c = _ctrl({"paused": False, "chunk_count": 10, "connections": conns})
    assert "reconnect" in c.status_text().lower()


def test_menu_has_reconnect_when_google_needs_action():
    conns = {"google": {"state": "needs_action", "detail": "Access expired — reconnect",
                        "last_verified": None}}
    c = _ctrl({"paused": False, "chunk_count": 1, "connections": conns,
               "version": "0.3.0"})
    labels = [label for (label, _h, _e) in c.menu_items()]
    assert any("Reconnect Google" in lbl for lbl in labels)
    assert any("0.3.0" in lbl for lbl in labels)


def test_menu_no_reconnect_when_all_ok():
    c = _ctrl({"paused": False, "chunk_count": 1, "connections": _OK})
    labels = [label for (label, _h, _e) in c.menu_items()]
    assert not any("Reconnect" in lbl for lbl in labels)
