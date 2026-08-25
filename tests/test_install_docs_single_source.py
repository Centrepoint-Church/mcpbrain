"""plugin/commands/install.md is the ONE place install commands are written.

README.md told users for months to clone `mcp-ops-brain` and run
`./install/setup.sh` — a repo name that is wrong and a script this repo does not
contain. Duplicated instructions rot silently; a test is the only thing that
notices.
"""
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_CANONICAL = _ROOT / "plugin" / "commands" / "install.md"
# DISTRIBUTION documents the auto-update mechanism, whose command line legitimately
# contains "uv tool install"; RELEASE-RUNBOOK is the maintainer's own procedure.
_MUST_NOT_INSTRUCT = ("README.md",)


def test_readme_does_not_reference_removed_installer_scripts():
    text = (_ROOT / "README.md").read_text()
    for dead in ("install/setup.sh", "install/setup.command", "install/setup.ps1",
                 "mcp-ops-brain"):
        assert dead not in text, f"README references {dead!r}, which does not exist"


def test_readme_points_at_the_install_command():
    assert "/mcpbrain:install" in (_ROOT / "README.md").read_text()


def test_readme_carries_no_second_copy_of_the_install_command():
    for rel in _MUST_NOT_INSTRUCT:
        assert "uv tool install" not in (_ROOT / rel).read_text(), \
            f"{rel} duplicates the install command; link to install.md instead"


def test_readme_describes_update_as_a_wheel_reinstall():
    text = (_ROOT / "README.md").read_text()
    assert "fast-forward" not in text, "update.py reinstalls from the wheel index, not git"


def test_canonical_install_command_keeps_the_daemon_alias():
    # Deliberate: the extra must STAY in the install command. It resolves against
    # both the pre-0.7.119 wheels (where fastembed is extra-only) and every wheel
    # after (where `daemon = []` is an empty alias and fastembed is a base dep).
    # Dropping it breaks every fresh install until a new wheel is published, with
    # no auto-update path back — `update.py`'s `_should_update` compares installed
    # vs latest and both are the same version until a release lands.
    assert '"mcpbrain[daemon]"' in _CANONICAL.read_text()
