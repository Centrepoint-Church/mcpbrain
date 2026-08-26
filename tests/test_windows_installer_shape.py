"""install.ps1 must not regrow the plan layer that did nothing.

Get-InstallPlan computed 'persistence-schtasks'/'persistence-startup' and
Invoke-InstallPlan discarded both; Test-Scheduler duplicated
agents._scheduler_available() while creating and deleting a real scheduled task
as a side effect, so every install probed the scheduler twice.
"""
from pathlib import Path

_PS1 = Path(__file__).parent.parent / "plugin" / "scripts" / "install.ps1"


def test_no_inert_persistence_planning():
    text = _PS1.read_text()
    assert "persistence-schtasks" not in text
    assert "persistence-startup" not in text
    assert "Test-Scheduler" not in text


def test_still_does_the_four_real_actions():
    text = _PS1.read_text()
    for token in ("Install-Uv", "Install-VcRedistX64", "Install-Mcpbrain", "mcpbrain setup"):
        assert token in text, token


def test_never_installs_the_arm64_redist():
    text = _PS1.read_text().lower()
    assert "vc_redist.arm64" not in text
    assert "vc_redist.x64.exe" in text


def test_keeps_the_uv_link_fallback():
    # A real ARM64 machine hit this: uv can fail to finalise the minor-version
    # link even though the x64 interpreter is fully extracted.
    text = _PS1.read_text()
    assert "uv python install" in text and "python.exe" in text
