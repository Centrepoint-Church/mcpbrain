"""Setup installers must install Claude Code (enrichment runs through it)."""
from pathlib import Path

INSTALL = Path(__file__).parent.parent / "install"


def test_unix_installers_install_claude_code():
    for name in ("setup.sh", "setup.command"):
        text = (INSTALL / name).read_text()
        assert "claude.ai/install.sh" in text, f"{name} must install Claude Code"
        assert "command -v claude" in text, f"{name} should skip if claude present"


def test_windows_installer_installs_claude_code():
    text = (INSTALL / "setup.ps1").read_text()
    assert "claude.ai/install.ps1" in text
    assert "Get-Command claude" in text
