"""Menu-bar tray for mcpbrain.

The daemon's lifecycle is owned by the OS login agent (launchd / systemd /
Task Scheduler), not the tray. The tray is a separate, optional process that
talks to the daemon over the loopback control API, so it is a status-and-control
*client*, not the daemon's owner. Quitting the tray closes the icon only; the
daemon keeps running under the login agent.

Two layers, so pystray stays out of the test path:

1.  TrayController — pure Python, no GUI imports. Wraps a ControlClient, holds
    the last status snapshot, and produces testable menu descriptions.
2.  run_tray(controller) — lazy-imports pystray inside the function body, so
    importing mcpbrain.tray never needs a display backend or pystray. Manual
    smoke only; not unit-tested.
"""

from __future__ import annotations

import webbrowser
from typing import TYPE_CHECKING

from mcpbrain.control_client import DaemonUnavailable

if TYPE_CHECKING:
    from mcpbrain.control_client import ControlClient


class TrayController:
    """Pure, GUI-free controller for the menu-bar tray.

    Wraps a ControlClient and caches the last status snapshot. refresh() polls
    the daemon; the menu and title read the cached snapshot so a single poll
    drives both. No pystray import here.
    """

    def __init__(self, client: "ControlClient") -> None:
        self._client = client
        self._last: dict | None = None
        self._available = False
        self._quit = False

    # -- polling --------------------------------------------------------------

    def refresh(self) -> None:
        """Poll the daemon's status, caching the result. Never raises."""
        try:
            self._last = self._client.status()
            self._available = True
        except DaemonUnavailable:
            self._last = None
            self._available = False

    # -- derived state --------------------------------------------------------

    def is_paused(self) -> bool:
        return bool(self._last and self._last.get("paused"))

    def should_exit(self) -> bool:
        return self._quit

    def review_count(self) -> int:
        """Return the number of open findings from the last status snapshot."""
        return int(self._last.get("open_findings") or 0) if self._last else 0

    def attention(self) -> list[dict]:
        """Connections that need the user to act, as [{connection, detail}]."""
        if not self._last:
            return []
        conns = self._last.get("connections") or {}
        return [{"connection": name, "detail": c.get("detail", "")}
                for name, c in conns.items() if c.get("state") == "needs_action"]

    def icon_state(self) -> str:
        """One of: unavailable | paused | attention | running."""
        if not self._available or self._last is None:
            return "unavailable"
        if self.attention():
            return "attention"
        if self._last.get("paused"):
            return "paused"
        return "running"

    def status_text(self) -> str:
        """Short, user-facing status line."""
        if not self._available or self._last is None:
            return "Daemon not running"
        att = self.attention()
        if att:
            return f"Needs attention: {att[0]['detail']}"
        if self._last.get("paused"):
            return "Paused"
        count = self._last.get("chunk_count")
        base = f"{count:,} items indexed" if isinstance(count, int) and count > 0 else "Running"
        n = self.review_count()
        return base + f" · {n} to review" if n > 0 else base

    # -- menu actions ---------------------------------------------------------

    def on_pause(self) -> None:
        try:
            self._client.pause()
        except DaemonUnavailable:
            pass
        self.refresh()

    def on_resume(self) -> None:
        try:
            self._client.resume()
        except DaemonUnavailable:
            pass
        self.refresh()

    def on_open_setup(self) -> None:
        """Open the local setup/status page in the browser."""
        url = self._client.wizard_url()
        if url:
            webbrowser.open(url)

    def on_open_dashboard(self) -> None:
        """Open the local today-dashboard in the browser."""
        url = self._client.dashboard_url()
        if url:
            webbrowser.open(url)

    def on_quit(self) -> None:
        """Close the tray icon. Does NOT stop the daemon (login agent owns it)."""
        self._quit = True

    def on_reconnect_google(self) -> None:
        try:
            self._client.reconnect_google()
        except DaemonUnavailable:
            pass

    # -- menu description (no pystray types) ----------------------------------

    def menu_items(self) -> list[tuple[str, object, bool]]:
        """Return a plain (label, handler, enabled) description of the menu."""
        if self.is_paused():
            toggle = ("Resume", self.on_resume, self._available)
        else:
            toggle = ("Pause", self.on_pause, self._available)
        items = [
            (self.status_text(), None, False),
            ("Open Dashboard", self.on_open_dashboard, True),
            toggle,
        ]
        if any(a["connection"] == "google" for a in self.attention()):
            items.append(("Reconnect Google", self.on_reconnect_google, self._available))
        items.append(("Open setup…", self.on_open_setup, True))
        ver = (self._last or {}).get("version")
        items.append((f"Up to date · v{ver}" if ver else "mcpbrain", None, False))
        items.append(("Quit", self.on_quit, True))
        return items


# ---------------------------------------------------------------------------
# Render layer (lazy pystray import — manual smoke only, not unit-tested)
# ---------------------------------------------------------------------------

def run_tray(controller: TrayController) -> None:  # pragma: no cover
    """Build and run a pystray Icon from controller.menu_items().

    Lazy-imports pystray (and PIL for the icon image) so importing mcpbrain.tray
    does not require a display backend. Blocks until the user picks Quit.

    The setup callback polls controller.refresh() every couple of seconds,
    refreshing the title and re-evaluating the menu so the status line and the
    Pause/Resume label track the daemon. The loop exits and stops the icon once
    controller.should_exit() is True (the user picked Quit).
    """
    import time
    import pystray
    from PIL import Image, ImageDraw

    _POLL_SECONDS = 3.0

    def _make_icon_image(size: int = 64) -> "Image.Image":
        img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        draw = ImageDraw.Draw(img)
        margin = size // 8
        draw.ellipse(
            [margin, margin, size - margin, size - margin],
            fill=(0, 102, 255, 255),  # electric-blue dot
        )
        return img

    def _menu_items() -> list["pystray.MenuItem"]:
        items = []
        for label, handler, enabled in controller.menu_items():
            action = handler if handler is not None else (lambda icon, item: None)
            items.append(pystray.MenuItem(label, action, enabled=enabled))
        return items

    def _setup(icon: "pystray.Icon") -> None:
        icon.visible = True
        last_n = 0
        while not controller.should_exit():
            controller.refresh()
            icon.title = f"mcpbrain — {controller.status_text()}"
            icon.update_menu()
            n = controller.review_count()
            if n > last_n:
                try:
                    icon.notify(f"{n} items to review", "mcpbrain")
                except Exception:
                    pass
            last_n = n
            time.sleep(_POLL_SECONDS)
        icon.stop()

    icon = pystray.Icon(
        "mcpbrain",
        _make_icon_image(),
        title="mcpbrain",
        menu=pystray.Menu(_menu_items),
    )

    # Menu-bar-only: mark the process an "accessory" app so it shows ONLY in the
    # status bar — no Dock icon, no foreground app presence. Without this the
    # framework Python.app build pops a Dock icon every time the tray
    # (re)starts. Set on the shared NSApplication (main thread, before the run
    # loop); pystray reuses the same shared app. Best-effort.
    try:
        from AppKit import NSApplication, NSApplicationActivationPolicyAccessory
        NSApplication.sharedApplication().setActivationPolicy_(
            NSApplicationActivationPolicyAccessory)
    except Exception:
        pass

    icon.run(_setup)


def main(argv=None) -> int:  # pragma: no cover - thin CLI entry
    """`mcpbrain tray` entry point: run the menu-bar tray against the daemon."""
    from mcpbrain.control_client import ControlClient
    run_tray(TrayController(ControlClient()))
    return 0
