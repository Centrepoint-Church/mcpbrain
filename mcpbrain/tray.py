"""System-tray controller for the ops-brain daemon.

Split into two layers to keep pystray out of the test path:

1.  TrayController  — pure Python, no GUI imports. Wraps a Daemon, tracks a
    user-facing status string, and provides testable menu-item descriptions.
    Import this anywhere.

2.  run_tray(controller)  — lazy-imports pystray inside the function body so
    importing mcpbrain.tray never requires a display backend or pystray install.
    Not unit-tested; manual smoke only.

Status precedence (documented here, mirrored in TrayController.status):
  1. "Stopped"  — daemon.stop() was called (sticky; overrides everything).
  2. "Paused"   — daemon.is_paused() is True (and not stopped).
  3. override   — a set_status("Syncing") or set_status("Error") call is active.
  4. "Running"  — default when none of the above apply.

The "Stopped" and "Paused" states are derived from the daemon's own flags so
they can never drift out of sync. The optional override is a hook for the daemon
to surface transient states (e.g. an in-progress sync cycle or a sync error)
without adding a callback dependency at construction time.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from mcpbrain.daemon import Daemon

_VALID_STATUSES = frozenset({"Running", "Paused", "Stopped", "Syncing", "Error"})
_OVERRIDE_STATUSES = frozenset({"Syncing", "Error"})  # set_status-only values


class TrayController:
    """Pure, GUI-free controller for the system tray.

    Wraps a Daemon and tracks a user-facing status string. The pystray layer
    calls these handlers; tests drive them directly. No pystray import here.

    Status precedence (see module docstring):
      Stopped > Paused > override (Syncing/Error) > Running
    """

    def __init__(self, daemon: "Daemon") -> None:
        self._daemon = daemon
        self._stopped = False
        self._override: str | None = None  # "Syncing" or "Error", or None

    # -- status ---------------------------------------------------------------

    def status(self) -> str:
        """Return the current user-facing status string.

        Precedence: Stopped > Paused > override > Running.

        "Stopped" is returned when the tray has quit (self._stopped) OR when
        the daemon was stopped via any external path (daemon.is_stopped()).
        This means run_tray's polling loop exits correctly even if daemon.stop()
        is called directly without going through on_quit().
        """
        if self._stopped or self._daemon.is_stopped():
            return "Stopped"
        if self._daemon.is_paused():
            return "Paused"
        if self._override is not None:
            return self._override
        return "Running"

    def set_status(self, status: str) -> None:
        """Set a transient override status (Syncing or Error).

        Only "Syncing" and "Error" are accepted as override values; to reflect
        Paused/Stopped/Running, call the corresponding action handler instead.
        Raises ValueError for anything not in the full valid-status set.

        The override is only visible when the daemon is not stopped or paused
        (per the precedence rules). It is NOT cleared automatically; the daemon
        or a caller should call set_status("Running") — or more accurately just
        call on_resume() — to clear it when the transient state ends. Passing
        a non-override valid status ("Running", "Paused", "Stopped") raises
        ValueError with a hint to use the appropriate handler instead.
        """
        if status not in _VALID_STATUSES:
            raise ValueError(
                f"Invalid status {status!r}. Must be one of: "
                f"{sorted(_VALID_STATUSES)}"
            )
        if status not in _OVERRIDE_STATUSES:
            raise ValueError(
                f"{status!r} cannot be set via set_status. "
                f"Use on_pause(), on_resume(), or on_quit() instead."
            )
        self._override = status

    # -- menu actions ---------------------------------------------------------

    def on_sync_now(self) -> None:
        """Trigger an immediate sync cycle on the daemon."""
        self._daemon.sync_now()

    def on_pause(self) -> None:
        """Pause the daemon and update status to Paused."""
        self._daemon.pause()
        self._override = None

    def on_resume(self) -> None:
        """Resume the daemon and update status to Running."""
        self._daemon.resume()
        self._override = None

    def on_quit(self) -> None:
        """Stop the daemon and mark status as Stopped (sticky)."""
        self._daemon.stop()
        self._stopped = True
        self._override = None

    # -- menu description (no pystray types) ----------------------------------

    def menu_items(self) -> list[tuple[str, object, bool]]:
        """Return a plain description of the tray menu.

        Each entry is a (label, handler, enabled) tuple. No pystray types are
        used so this method is fully unit-testable. The pystray render layer maps
        these to real MenuItem objects.

        Menu shape:
          - "Sync now"       always present, enabled when not stopped
          - "Pause"/"Resume" toggles based on daemon.is_paused(); disabled when stopped
          - "Quit"           always present and always enabled
        """
        running = not self._stopped
        paused = self._daemon.is_paused()

        pause_label = "Resume" if paused else "Pause"
        pause_handler = self.on_resume if paused else self.on_pause

        return [
            ("Sync now", self.on_sync_now, running),
            (pause_label, pause_handler, running),
            ("Quit", self.on_quit, True),
        ]


# ---------------------------------------------------------------------------
# Render layer (lazy pystray import — manual smoke only, not unit-tested)
# ---------------------------------------------------------------------------

def run_tray(controller: TrayController) -> None:
    """Build and run a pystray Icon from controller.menu_items().

    Lazy-imports pystray (and PIL for the icon image) so importing mcpbrain.tray
    does not require a display backend. This function blocks until the user quits.

    Manual smoke only — not unit-tested. The setup callback polls
    controller.status() every couple of seconds, refreshing the tooltip and
    re-evaluating the menu so the Pause/Resume label tracks state. The loop
    exits and stops the icon once status() reports "Stopped".
    """
    import time                          # local: only the manual-smoke path needs it
    import pystray                       # lazy — GUI dep, may not exist on headless boxes
    from PIL import Image, ImageDraw     # pillow; present if pystray is installed

    _POLL_SECONDS = 2.0

    def _make_icon_image(size: int = 64) -> "Image.Image":
        """Minimal 64x64 PNG icon — a plain circle on a transparent background."""
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        margin = size // 8
        draw.ellipse(
            [margin, margin, size - margin, size - margin],
            fill=(70, 130, 180, 255),  # steel-blue dot
        )
        return img

    def _menu_items() -> list["pystray.MenuItem"]:
        # Re-evaluated on every icon.update_menu() so the Pause/Resume toggle and
        # enabled flags reflect the current controller state.
        return [
            pystray.MenuItem(label, handler, enabled=enabled)
            for label, handler, enabled in controller.menu_items()
        ]

    def _setup(icon: "pystray.Icon") -> None:
        # pystray calls this once on its own thread after the icon is shown.
        # We keep it alive as the polling loop so the tooltip and menu stay live.
        # status() is cached once per iteration so title and the exit-check read
        # the same snapshot (avoids a mid-transition inconsistency between the two
        # calls that existed before this fix).
        icon.visible = True
        while True:
            s = controller.status()
            icon.title = f"ops-brain: {s}"
            icon.update_menu()
            if s == "Stopped":
                break
            time.sleep(_POLL_SECONDS)
        icon.stop()

    icon_image = _make_icon_image()
    icon = pystray.Icon(
        "ops-brain",
        icon_image,
        title=f"ops-brain: {controller.status()}",
        menu=pystray.Menu(_menu_items),  # callable → pystray re-evaluates on update_menu()
    )
    icon.run(_setup)
