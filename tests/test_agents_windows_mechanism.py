# tests/test_agents_windows_mechanism.py  (new)
from mcpbrain import agents


def test_mechanism_schtasks_when_available():
    assert agents.win_persistence_mechanism(probe=True) == "schtasks"


def test_mechanism_startup_when_blocked():
    assert agents.win_persistence_mechanism(probe=False) == "startup"


def test_shim_runs_absolute_mcpbrain_exe():
    content = agents._win_shim_content(
        mcpbrain_bin=r"C:\bin\mcpbrain.exe",
        home=r"C:\Users\j\AppData\Roaming\mcpbrain",
        subcommand="daemon",
    )
    assert '""C:\\bin\\mcpbrain.exe"" daemon' in content
    assert "-m mcpbrain" not in content   # no pythonw module form


def test_startup_shortcut_target():
    wscript, args = agents.startup_shortcut_target(
        shim_path=r"C:\Users\j\AppData\Roaming\mcpbrain\agents\mcpbrain.vbs",
    )
    assert wscript.lower().endswith("wscript.exe")
    assert "mcpbrain.vbs" in args


def test_tray_uses_startup_when_scheduler_blocked(monkeypatch):
    calls = {}
    monkeypatch.setattr(agents, "win_persistence_mechanism", lambda probe=None: "startup")
    monkeypatch.setattr(agents, "_install_startup_shortcut",
                        lambda task, **kw: calls.setdefault("startup", []).append(task))
    monkeypatch.setattr(agents, "_win_shim_path", lambda home, task: __import__("pathlib").Path(home) / f"{task}.vbs")
    monkeypatch.setattr("pathlib.Path.write_text", lambda self, *a, **k: None)
    monkeypatch.setattr("pathlib.Path.mkdir", lambda self, *a, **k: None)
    agents._install_schtasks_tray(mcpbrain_bin=r"C:\bin\mcpbrain.exe", home=r"C:\home")
    assert "mcpbrain-tray" in calls.get("startup", [])
