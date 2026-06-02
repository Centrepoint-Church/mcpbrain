import mcpbrain.update as upd

def test_update_runs_pull_reinstall_restart(monkeypatch):
    calls = []
    monkeypatch.setattr(upd, "_run", lambda cmd, **k: calls.append(cmd) or ("", 0))
    monkeypatch.setattr(upd, "_repo_dir", lambda: "/repo")
    monkeypatch.setattr(upd, "_restart_agent", lambda: calls.append(["restart"]))
    upd.main([])
    assert ["git","-C","/repo","pull","--ff-only"] in calls
    assert any("uv" in c and "install" in c for c in calls) and ["restart"] in calls

def test_non_fast_forward_aborts(monkeypatch):
    monkeypatch.setattr(upd, "_repo_dir", lambda: "/repo")
    monkeypatch.setattr(upd, "_run",
        lambda cmd, **k: ("fatal: Not possible to fast-forward", 1) if "pull" in cmd else ("", 0))
    assert upd.main([]) != 0

def test_failed_reinstall_aborts_before_restart(monkeypatch):
    calls = []

    def fake_run(cmd, **k):
        calls.append(cmd)
        if "uv" in cmd:
            return ("error: build failed", 1)
        return ("", 0)

    monkeypatch.setattr(upd, "_run", fake_run)
    monkeypatch.setattr(upd, "_repo_dir", lambda: "/repo")
    monkeypatch.setattr(upd, "_restart_agent", lambda: calls.append(["restart"]))

    rc = upd.main([])
    assert rc != 0
    assert ["restart"] not in calls
