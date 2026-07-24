"""Tests for TrayController (client-backed, GUI-free).

The controller wraps a ControlClient and never imports pystray. run_tray (the
pystray render layer) is manual-smoke only and not tested here.
"""

import pathlib
import sys

from mcpbrain.control_client import DaemonUnavailable
from mcpbrain.tray import TrayController


class FakeClient:
    """Stand-in for ControlClient. `up=False` simulates a stopped daemon."""

    def __init__(self, up=True, paused=False, count=7, enriched=0, open_findings=0):
        self.up = up
        self.paused = paused
        self.count = count
        self.enriched = enriched
        self.open_findings = open_findings
        self.calls = []

    def status(self):
        self.calls.append("status")
        if not self.up:
            raise DaemonUnavailable("no daemon")
        return {"paused": self.paused, "chunk_count": self.count,
                "enriched_count": self.enriched,
                "google_connected": True, "granted_scopes": [], "enrich_enabled": False,
                "open_findings": self.open_findings}

    def pause(self):
        self.calls.append("pause")
        self.paused = True

    def resume(self):
        self.calls.append("resume")
        self.paused = False

    def wizard_url(self):
        return "http://127.0.0.1:50000/" if self.up else ""

    def dashboard_url(self):
        return "http://127.0.0.1:50000/dashboard" if self.up else ""


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


def test_menu_shows_indexed_and_enriched_counts():
    # Running: first two lines are the Indexed / Enriched counts, disabled.
    items = _ctrl(count=2249, enriched=1337).menu_items()
    assert items[0] == ("Indexed: 2,249", None, False)
    assert items[1] == ("Enriched: 1,337", None, False)


def test_menu_omits_pause_and_setup():
    # Pause/Resume and Open setup no longer live in the tray menu.
    labels = [lab for (lab, _, _) in _ctrl().menu_items()]
    assert not any("Pause" in lab or "Resume" in lab for lab in labels)
    assert not any("setup" in lab.lower() for lab in labels)


def test_menu_when_daemon_down():
    # Down: a single "Daemon not running" line (disabled); Quit still enabled.
    down = _ctrl(up=False).menu_items()
    assert down[0] == ("Daemon not running", None, False)
    assert not any(lab.startswith("Enriched:") for (lab, _, _) in down)
    assert any(lab == "Quit" and e for (lab, _, e) in down)


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


# ---------------------------------------------------------------------------
# dashboard_url and Open Dashboard menu item
# ---------------------------------------------------------------------------

def test_dashboard_url_returns_dashboard_path(tmp_path):
    """ControlClient pointed at a fake home with port+token files returns a URL
    ending in /dashboard."""
    from mcpbrain.control_client import ControlClient
    (tmp_path / "control_port").write_text("51234\n")
    (tmp_path / "control_token").write_text("tok\n")
    client = ControlClient(home=tmp_path)
    url = client.dashboard_url()
    assert url.endswith("/dashboard"), f"Expected URL ending in /dashboard, got: {url!r}"


def test_dashboard_url_returns_empty_when_unavailable(tmp_path):
    """ControlClient with no port file returns '' from dashboard_url()."""
    from mcpbrain.control_client import ControlClient
    client = ControlClient(home=tmp_path)
    assert client.dashboard_url() == ""


def test_menu_has_open_dashboard():
    """menu_items() has an always-enabled 'Open Dashboard' item after the counts."""
    items = _ctrl().menu_items()
    assert items[2][0] == "Open Dashboard"
    assert items[2][2] is True  # always enabled


def test_on_open_dashboard_calls_webbrowser(monkeypatch):
    """on_open_dashboard() calls webbrowser.open with a URL containing /dashboard."""
    import mcpbrain.tray as tray
    opened = {}
    monkeypatch.setattr(tray.webbrowser, "open", lambda u: opened.setdefault("url", u))
    _ctrl().on_open_dashboard()
    assert "/dashboard" in opened.get("url", ""), (
        f"Expected URL containing /dashboard, got: {opened!r}"
    )


def test_status_text_shows_review_count():
    c = TrayController(FakeClient(count=5, open_findings=3))
    c.refresh()
    assert "3 to review" in c.status_text()


def test_review_count_zero_not_shown():
    c = TrayController(FakeClient(count=5, open_findings=0))
    c.refresh()
    assert "review" not in c.status_text()


def test_review_count_accessor():
    c = TrayController(FakeClient(open_findings=2))
    c.refresh()
    assert c.review_count() == 2


def test_tray_title_within_windows_tooltip_limit():
    """Windows NOTIFYICONDATA tooltips truncate/reject beyond 127 chars.

    The tray's static pystray Icon(title=...) is a fixed literal ("mcpbrain"),
    not the dynamic status string — so this guards the literal, not the status
    text that gets set later via icon.title assignment. Regression guard for
    any future move to a longer static title.
    """
    import re

    src = pathlib.Path("mcpbrain/tray.py").read_text(encoding="utf-8")
    m = re.search(r'title\s*=\s*"([^"]*)"', src)
    assert m and len(m.group(1)) <= 127
