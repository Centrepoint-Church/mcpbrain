# tests/test_agents_windows_mechanism.py  (new)
from mcpbrain import agents


def test_mechanism_schtasks_when_available():
    assert agents.win_persistence_mechanism(probe=True) == "schtasks"


def test_mechanism_startup_when_blocked():
    assert agents.win_persistence_mechanism(probe=False) == "startup"


def test_shim_runs_signed_interpreter():
    content = agents._win_shim_content(
        mcpbrain_bin=r"C:\bin\mcpbrain.exe",
        home=r"C:\Users\j\AppData\Roaming\mcpbrain",
        subcommand="daemon",
        python_bin=r"C:\py\pythonw.exe",
    )
    assert "-m mcpbrain daemon" in content
    assert "pythonw.exe" in content
    assert "mcpbrain.exe daemon" not in content   # not the unsigned trampoline


def test_startup_shortcut_target():
    wscript, args = agents.startup_shortcut_target(
        python_bin=r"C:\py\pythonw.exe",
        shim_path=r"C:\Users\j\AppData\Roaming\mcpbrain\agents\mcpbrain.vbs",
    )
    assert wscript.lower().endswith("wscript.exe")
    assert "mcpbrain.vbs" in args
