"""Tests for TrayController state-machine logic (Task 3.2).

Design notes:
- Uses a minimal FakeDaemon (not the real Daemon) to avoid needing a Store/
  embedder setup. The FakeDaemon exposes the same public API as the real Daemon
  (pause/resume/is_paused/sync_now/stop) with recorded-call tracking.
- pystray must NOT be imported as a side-effect of importing mcpbrain.tray.
  One test asserts "pystray" not in sys.modules immediately after import.
- run_tray() is not exercised here (GUI, manual smoke only).
"""

import sys
import importlib

import pytest


# ---------------------------------------------------------------------------
# Ensure pystray is absent before import (module-level isolation)
# ---------------------------------------------------------------------------
# Remove any prior import of mcpbrain.tray or pystray so the no-pystray assert
# is clean even when pytest reorders test collection.
for _mod in list(sys.modules):
    if _mod == "pystray" or _mod.startswith("mcpbrain.tray"):
        del sys.modules[_mod]

from mcpbrain.tray import TrayController  # noqa: E402  (must come after purge above)


# ---------------------------------------------------------------------------
# Minimal fake daemon (records calls, mirrors Daemon's public API)
# ---------------------------------------------------------------------------

class FakeDaemon:
    """Minimal stand-in for mcpbrain.daemon.Daemon.

    Tracks calls to pause/resume/sync_now/stop so tests can assert on them.
    is_paused() reflects the pause/resume state.
    is_stopped() reflects whether stop() has been called (mirrors the real
    Daemon.is_stopped() which returns self._stop.is_set()).
    stop() sets a stopped flag; sync_now() sets a wake flag.
    """

    def __init__(self):
        self._paused = False
        self._stopped = False
        self.stopped = False   # legacy public attribute; kept for existing assertions
        self.wake_count = 0

    def pause(self) -> None:
        self._paused = True

    def resume(self) -> None:
        self._paused = False

    def is_paused(self) -> bool:
        return self._paused

    def is_stopped(self) -> bool:
        return self._stopped

    def sync_now(self) -> None:
        self.wake_count += 1

    def stop(self) -> None:
        self._stopped = True
        self.stopped = True    # keep legacy attribute in sync


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def daemon():
    return FakeDaemon()


@pytest.fixture
def ctrl(daemon):
    return TrayController(daemon)


# ---------------------------------------------------------------------------
# pystray absent after import
# ---------------------------------------------------------------------------

def test_import_does_not_import_pystray():
    """Importing mcpbrain.tray must not pull in pystray (GUI dep)."""
    # The module was already imported at the top of this file.
    # pystray should NOT be in sys.modules.
    assert "pystray" not in sys.modules, (
        "importing mcpbrain.tray should not import pystray; "
        "it must be lazy-imported only inside run_tray()"
    )


# ---------------------------------------------------------------------------
# Initial state
# ---------------------------------------------------------------------------

def test_initial_status_is_running(ctrl):
    assert ctrl.status() == "Running"


def test_initial_menu_shows_pause(ctrl):
    labels = [label for label, _, _ in ctrl.menu_items()]
    assert "Pause" in labels
    assert "Resume" not in labels


# ---------------------------------------------------------------------------
# on_pause
# ---------------------------------------------------------------------------

def test_on_pause_sets_daemon_paused(ctrl, daemon):
    ctrl.on_pause()
    assert daemon.is_paused() is True


def test_on_pause_status_is_paused(ctrl):
    ctrl.on_pause()
    assert ctrl.status() == "Paused"


def test_on_pause_menu_shows_resume(ctrl):
    ctrl.on_pause()
    labels = [label for label, _, _ in ctrl.menu_items()]
    assert "Resume" in labels
    assert "Pause" not in labels


def test_on_pause_menu_sync_now_still_enabled(ctrl):
    """Sync now stays enabled when paused (disabled only after quit/stop)."""
    ctrl.on_pause()
    items = {label: enabled for label, _, enabled in ctrl.menu_items()}
    assert items["Sync now"] is True


# ---------------------------------------------------------------------------
# on_resume
# ---------------------------------------------------------------------------

def test_on_resume_clears_daemon_pause(ctrl, daemon):
    ctrl.on_pause()
    ctrl.on_resume()
    assert daemon.is_paused() is False


def test_on_resume_status_is_running(ctrl):
    ctrl.on_pause()
    ctrl.on_resume()
    assert ctrl.status() == "Running"


def test_on_resume_menu_shows_pause_again(ctrl):
    ctrl.on_pause()
    ctrl.on_resume()
    labels = [label for label, _, _ in ctrl.menu_items()]
    assert "Pause" in labels
    assert "Resume" not in labels


# ---------------------------------------------------------------------------
# on_sync_now
# ---------------------------------------------------------------------------

def test_on_sync_now_wakes_daemon(ctrl, daemon):
    assert daemon.wake_count == 0
    ctrl.on_sync_now()
    assert daemon.wake_count == 1


def test_on_sync_now_multiple_calls_accumulate(ctrl, daemon):
    ctrl.on_sync_now()
    ctrl.on_sync_now()
    assert daemon.wake_count == 2


# ---------------------------------------------------------------------------
# on_quit
# ---------------------------------------------------------------------------

def test_on_quit_stops_daemon(ctrl, daemon):
    ctrl.on_quit()
    assert daemon.stopped is True


def test_on_quit_status_is_stopped(ctrl):
    ctrl.on_quit()
    assert ctrl.status() == "Stopped"


def test_on_quit_status_stays_stopped_even_if_paused(ctrl, daemon):
    """Stopped takes precedence over Paused in the status hierarchy."""
    ctrl.on_pause()
    ctrl.on_quit()
    assert ctrl.status() == "Stopped"


def test_on_quit_menu_sync_now_disabled(ctrl):
    ctrl.on_quit()
    items = {label: enabled for label, _, enabled in ctrl.menu_items()}
    assert items["Sync now"] is False


def test_on_quit_menu_pause_resume_disabled(ctrl):
    ctrl.on_quit()
    items = {label: enabled for label, _, enabled in ctrl.menu_items()}
    # whichever toggle label is shown, it must be disabled after quit
    toggle_label = next(
        label for label in items if label in ("Pause", "Resume")
    )
    assert items[toggle_label] is False


def test_on_quit_menu_quit_still_enabled(ctrl):
    ctrl.on_quit()
    items = {label: enabled for label, _, enabled in ctrl.menu_items()}
    assert items["Quit"] is True


# ---------------------------------------------------------------------------
# set_status — override states (Syncing / Error)
# ---------------------------------------------------------------------------

def test_set_status_syncing_reflected(ctrl):
    ctrl.set_status("Syncing")
    assert ctrl.status() == "Syncing"


def test_set_status_error_reflected(ctrl):
    ctrl.set_status("Error")
    assert ctrl.status() == "Error"


def test_set_status_override_hidden_when_paused(ctrl):
    """Paused takes precedence over Syncing/Error override."""
    ctrl.set_status("Syncing")
    ctrl.on_pause()
    assert ctrl.status() == "Paused"


def test_set_status_override_hidden_when_stopped(ctrl):
    """Stopped takes precedence over Syncing/Error override."""
    ctrl.set_status("Error")
    ctrl.on_quit()
    assert ctrl.status() == "Stopped"


def test_on_resume_clears_override(ctrl):
    """Resuming from paused should also clear any prior Syncing/Error override."""
    ctrl.set_status("Error")
    ctrl.on_pause()
    ctrl.on_resume()
    # Override was cleared; should be Running (not Error)
    assert ctrl.status() == "Running"


def test_set_status_invalid_raises_value_error(ctrl):
    with pytest.raises(ValueError, match="Invalid status"):
        ctrl.set_status("Napping")


def test_set_status_running_raises_value_error(ctrl):
    """Running is valid as a status string but cannot be set via set_status."""
    with pytest.raises(ValueError, match="on_resume"):
        ctrl.set_status("Running")


def test_set_status_paused_raises_value_error(ctrl):
    with pytest.raises(ValueError, match="on_pause"):
        ctrl.set_status("Paused")


def test_set_status_stopped_raises_value_error(ctrl):
    with pytest.raises(ValueError, match="on_quit"):
        ctrl.set_status("Stopped")


# ---------------------------------------------------------------------------
# menu_items structure
# ---------------------------------------------------------------------------

def test_menu_items_always_has_three_entries(ctrl):
    assert len(ctrl.menu_items()) == 3


def test_menu_items_always_has_quit(ctrl):
    labels = [label for label, _, _ in ctrl.menu_items()]
    assert "Quit" in labels


def test_menu_items_handlers_are_callable(ctrl):
    for label, handler, enabled in ctrl.menu_items():
        assert callable(handler), f"handler for {label!r} is not callable"


def test_menu_items_enabled_flags_are_bool(ctrl):
    for label, handler, enabled in ctrl.menu_items():
        assert isinstance(enabled, bool), f"enabled for {label!r} is not bool"


def test_menu_items_sync_now_enabled_when_running(ctrl):
    items = {label: enabled for label, _, enabled in ctrl.menu_items()}
    assert items["Sync now"] is True


def test_menu_items_quit_always_enabled_when_running(ctrl):
    items = {label: enabled for label, _, enabled in ctrl.menu_items()}
    assert items["Quit"] is True


def test_menu_pause_handler_calls_on_pause(ctrl, daemon):
    """The handler for 'Pause' in the menu should actually call daemon.pause()."""
    items = {label: handler for label, handler, _ in ctrl.menu_items()}
    items["Pause"]()
    assert daemon.is_paused() is True


def test_menu_resume_handler_calls_on_resume(ctrl, daemon):
    """The handler for 'Resume' in the menu should actually call daemon.resume()."""
    ctrl.on_pause()
    items = {label: handler for label, handler, _ in ctrl.menu_items()}
    items["Resume"]()
    assert daemon.is_paused() is False


# ---------------------------------------------------------------------------
# Fix I1 — controller.status() reflects daemon stopped via any path
# ---------------------------------------------------------------------------

def test_status_stopped_when_daemon_stopped_directly(ctrl, daemon):
    """status() must return 'Stopped' when daemon.stop() is called directly,
    without going through controller.on_quit().

    Before the fix, status() only checked self._stopped (set only in on_quit()).
    This test would have returned 'Running' before the fix.
    """
    # Sanity: initially Running, and the tray's own _stopped flag is False.
    assert ctrl.status() == "Running"
    assert ctrl._stopped is False

    # Stop the daemon directly — not via the tray's on_quit().
    daemon.stop()

    # The controller must now report Stopped because daemon.is_stopped() is True.
    assert ctrl.status() == "Stopped"


def test_status_stopped_precedence_over_paused_when_daemon_stopped_directly(ctrl, daemon):
    """Stopped still takes precedence over Paused when the daemon was stopped
    externally (not via on_quit)."""
    daemon.pause()
    daemon.stop()
    assert ctrl.status() == "Stopped"
