def test_relaunch_windows(monkeypatch):
    import mcpbrain.desktop as desktop
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setattr(desktop, "_windows_claude_exe", lambda: r"C:\x\Claude.exe")
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
    monkeypatch.setattr(desktop.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(desktop.time, "sleep", lambda *_: None)
    res = desktop.relaunch_claude_desktop()
    assert res["relaunched"] is False
    assert "manually" in res["detail"]
