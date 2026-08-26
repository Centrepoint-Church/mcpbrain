import subprocess


def test_relaunch_windows(monkeypatch):
    import mcpbrain.desktop as desktop
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_claude_exe", lambda: r"C:\x\Claude.exe")
    monkeypatch.setattr(desktop, "_claude_running", lambda: False)
    ran = []
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: ran.append(("run", a[0])))
    monkeypatch.setattr(desktop.subprocess, "Popen", lambda *a, **k: ran.append(("popen", a[0])))
    monkeypatch.setattr(desktop.time, "sleep", lambda *_: None)
    res = desktop.relaunch_claude_desktop()
    assert res["relaunched"] is True
    assert any(kind == "popen" for kind, _ in ran)

def test_relaunch_unresolved_exe_is_graceful(monkeypatch):
    import mcpbrain.desktop as desktop
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_claude_exe", lambda: None)
    monkeypatch.setattr(desktop, "_claude_running", lambda: False)
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(desktop.time, "sleep", lambda *_: None)
    res = desktop.relaunch_claude_desktop()
    assert res["relaunched"] is False
    assert "manually" in res["detail"]


def test_quit_survives_hung_quit_command(monkeypatch):
    """A quit command that hangs past the subprocess timeout must not propagate
    as an unhandled TimeoutExpired — it should fall through to the same
    proceed-anyway logic the poll-loop timeout already uses."""
    import mcpbrain.desktop as desktop

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 0))

    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.subprocess, "run", hang)
    # The app has actually exited by the time we poll, even though the quit
    # command itself hung — proves the timeout was swallowed, not treated as
    # "still running forever".
    monkeypatch.setattr(desktop, "_claude_running", lambda: False)
    monkeypatch.setattr(desktop.time, "sleep", lambda *_: None)

    res = desktop.quit_claude_desktop()
    assert res == {"quit": True, "detail": "Claude Desktop exited"}


def test_quit_hung_command_and_still_running_proceeds_anyway(monkeypatch):
    """If the quit command hangs AND the app never actually exits, quit_claude_desktop
    still returns its existing "proceed anyway" dict rather than raising or blocking
    forever."""
    import mcpbrain.desktop as desktop

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 0))

    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.subprocess, "run", hang)
    monkeypatch.setattr(desktop, "_claude_running", lambda: True)
    monkeypatch.setattr(desktop.time, "sleep", lambda *_: None)

    res = desktop.quit_claude_desktop()
    assert res == {"quit": False, "detail": "Claude Desktop did not exit in time"}


def test_claude_running_timeout_assumes_not_running(monkeypatch):
    """A hung process-table check (pgrep/tasklist) must not propagate — the safer
    default is to assume the app isn't running, since callers treat False as
    'safe to proceed'."""
    import mcpbrain.desktop as desktop

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 0))

    monkeypatch.setattr(desktop.sys, "platform", "darwin")
    monkeypatch.setattr(desktop.subprocess, "run", hang)
    assert desktop._claude_running() is False

    monkeypatch.setattr(desktop.sys, "platform", "win32")
    assert desktop._claude_running() is False


def test_launch_win32_popen_raises_returns_failure_dict(monkeypatch):
    """launch_claude_desktop() itself must never raise: on win32, subprocess.Popen
    can raise OSError/PermissionError for a bad exe path — that must come back as
    a {"launched": False, ...} dict, not propagate to relaunch_claude_desktop's
    outer except (which fires too late, after the app has already been quit)."""
    import mcpbrain.desktop as desktop

    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_claude_exe", lambda: r"C:\bad\Claude.exe")

    def boom(*a, **k):
        raise OSError("access denied")

    monkeypatch.setattr(desktop.subprocess, "Popen", boom)

    res = desktop.launch_claude_desktop()
    assert res["launched"] is False
    assert "could not restart Claude Desktop" in res["detail"]
    assert "manually" in res["detail"]


def test_launch_darwin_run_raises_returns_failure_dict(monkeypatch):
    """On darwin, subprocess.run(check=False) doesn't raise for a nonzero exit,
    but a true OS-level failure (e.g. `open` missing) still raises — that must
    also come back as a failure dict rather than propagate."""
    import mcpbrain.desktop as desktop

    monkeypatch.setattr(desktop.sys, "platform", "darwin")

    def boom(*a, **k):
        raise FileNotFoundError("open: no such file or directory")

    monkeypatch.setattr(desktop.subprocess, "run", boom)

    res = desktop.launch_claude_desktop()
    assert res["launched"] is False
    assert "could not restart Claude Desktop" in res["detail"]


def test_launch_darwin_run_timeout_returns_failure_dict(monkeypatch):
    """A hung `open -a Claude` must not block the caller forever."""
    import mcpbrain.desktop as desktop

    monkeypatch.setattr(desktop.sys, "platform", "darwin")

    def hang(*a, **k):
        raise subprocess.TimeoutExpired(cmd=a[0], timeout=k.get("timeout", 0))

    monkeypatch.setattr(desktop.subprocess, "run", hang)

    res = desktop.launch_claude_desktop()
    assert res["launched"] is False
    assert "manually" in res["detail"]
