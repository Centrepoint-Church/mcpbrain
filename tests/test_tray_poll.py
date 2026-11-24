"""The tray poll loop touches the icon only through the main-thread dispatcher.

macOS 27.2 kills a process that mutates an NSStatusItem off the main thread
(SIGTRAP in ``-[NSStatusItem setMenu:]``). pystray runs the poll loop on a
background thread, so every icon mutation must go through ``on_main``.
"""

from __future__ import annotations

from mcpbrain.control_client import DaemonUnavailable
from mcpbrain.tray import TrayController, _main_thread_dispatcher, _poll_loop


class _Client:
    def __init__(self, snapshots):
        self._snaps = list(snapshots)

    def status(self):
        snap = self._snaps.pop(0) if len(self._snaps) > 1 else self._snaps[0]
        if snap is None:
            raise DaemonUnavailable("down")
        return snap


class _GuardedIcon:
    """Records mutations; raises if one happens outside the dispatcher."""

    def __init__(self, gate):
        object.__setattr__(self, "_gate", gate)
        object.__setattr__(self, "log", [])

    def __setattr__(self, name, value):
        assert self._gate["on_main"], f"icon.{name} set off the main thread"
        self.log.append(("set", name, value))

    def update_menu(self):
        assert self._gate["on_main"], "update_menu called off the main thread"
        self.log.append(("update_menu",))

    def notify(self, message, title=None):
        assert self._gate["on_main"], "notify called off the main thread"
        self.log.append(("notify", message))

    def stop(self):
        assert self._gate["on_main"], "stop called off the main thread"
        self.log.append(("stop",))


def _run(snapshots, passes):
    gate = {"on_main": False}
    posted = []

    def on_main(fn, *a, **kw):
        # Deferred like AppHelper.callAfter: run later, inside the gate.
        posted.append((fn, a, kw))

    ctrl = TrayController(_Client(snapshots))
    icon = _GuardedIcon(gate)
    count = {"n": 0}

    def sleep(_s):
        count["n"] += 1
        if count["n"] >= passes:
            ctrl.on_quit()

    _poll_loop(ctrl, icon, on_main=on_main, make_image=lambda s: f"img:{s}",
               sleep=sleep, interval=0)

    # Drain the "main thread" queue only after the loop has finished, so a
    # callback reading loop variables late would show the wrong value.
    gate["on_main"] = True
    for fn, a, kw in posted:
        fn(*a, **kw)
    return icon.log


def test_poll_loop_never_touches_icon_off_main_thread():
    log = _run([{"chunk_count": 5, "connections": {}}], passes=3)
    kinds = [e[0] for e in log]
    assert ("set", "visible", True) == log[0]
    assert kinds.count("update_menu") == 3
    assert log[-1] == ("stop",)


def test_poll_loop_binds_values_at_post_time():
    snaps = [None, {"chunk_count": 7, "connections": {}}]
    log = _run(snaps, passes=2)
    icons = [e[2] for e in log if e[:2] == ("set", "icon")]
    titles = [e[2] for e in log if e[:2] == ("set", "title")]
    assert icons == ["img:unavailable", "img:running"]
    assert titles == ["mcpbrain — Daemon not running", "mcpbrain — 7 items indexed"]


def test_poll_loop_notifies_through_dispatcher():
    snap = {"chunk_count": 1, "connections": {
        "google": {"state": "needs_action", "detail": "Google sign-in expired"}}}
    log = _run([snap], passes=2)
    assert [e for e in log if e[0] == "notify"] == [("notify", "Google sign-in expired")]


def test_darwin_dispatcher_is_appkit_main_thread_call():
    import sys
    import pytest
    if sys.platform != "darwin":
        pytest.skip("pyobjc is macOS-only")
    from PyObjCTools import AppHelper
    assert _main_thread_dispatcher("darwin") is AppHelper.callAfter


def test_other_platforms_run_inline():
    seen = []
    _main_thread_dispatcher("linux")(seen.append, 1)
    _main_thread_dispatcher("win32")(seen.append, 2)
    assert seen == [1, 2]
