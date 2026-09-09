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


def test_install_docs_name_the_tenant_organisation():
    # README's cold-start block duplicates plugin/INSTALL.md's rather than linking
    # to it. They agree today; nothing else would notice when they stop. The thing
    # to pin is the ORG NAME, because that is what a user filters the app's plugin
    # catalogue by — and it is what a fork must change. Derived from tenant.json so
    # this keeps working in a fork instead of failing there by construction.
    from mcpbrain import tenant
    prof = tenant.require()
    for rel in ("README.md", "plugin/INSTALL.md"):
        text = (_ROOT / rel).read_text()
        assert prof.display_name in text, (
            f"{rel} must name {prof.display_name!r} — it is what users filter the "
            f"plugin catalogue by")
        assert "Browse plugins" in text, f"{rel} must describe the catalogue flow"


def test_install_docs_do_not_recommend_adding_the_marketplace_by_hand():
    """`claude plugin marketplace add <owner>/<repo>` is the WRONG path for this
    plugin and the docs told people to use it for months.

    The plugin ships through claude.ai organization settings, which REQUIRES the
    marketplace repository to be private or internal — org sync reads it via the
    Claude GitHub App and packages the plugin per user, so nobody needs repo
    access. Adding a private marketplace by hand instead needs every person to
    hold git credentials (GitHub shorthand clones over SSH by default), and
    background refreshes disable credential helpers, so the clone silently stops
    updating and pins them to whatever they first cloned.

    Users install from Customize -> Plugins -> Browse plugins -> filter by org.
    """
    for rel in ("README.md", "plugin/INSTALL.md"):
        for n, line in enumerate((_ROOT / rel).read_text().splitlines(), 1):
            stripped = line.strip()
            # Allowed inside prose that explains why NOT to use it; banned as an
            # instruction, i.e. at the start of a line or in a fenced command.
            if stripped.startswith("claude plugin marketplace add") or \
                    stripped.startswith("claude plugin install mcpbrain@"):
                raise AssertionError(
                    f"{rel}:{n} instructs `{stripped}` — org-settings distribution "
                    f"requires a private marketplace repo, so this path does not "
                    f"work for users. Point them at the app's plugin catalogue.")


def test_canonical_install_command_keeps_the_daemon_alias():
    # Deliberate: the extra must STAY in the install command. It resolves against
    # both the pre-0.7.119 wheels (where fastembed is extra-only) and every wheel
    # after (where `daemon = []` is an empty alias and fastembed is a base dep).
    # Dropping it breaks every fresh install until a new wheel is published, with
    # no auto-update path back — `update.py`'s `_should_update` compares installed
    # vs latest and both are the same version until a release lands.
    assert '"mcpbrain[daemon]"' in _CANONICAL.read_text()


def test_every_fresh_install_command_keeps_the_daemon_alias():
    """Any index-based `uv tool install ... --force` must carry `mcpbrain[daemon]`.

    This exact defect appeared three times from one root cause: drop the extra
    from a fresh-install command and, until a wheel built with fastembed in base
    deps is newest on the index, that command installs a brain with NO embedder —
    mcp-server fails at startup, recall returns empty, and the daily auto-update
    cannot repair it (installed == latest, so `_should_update` is False).

    `"mcpbrain[daemon]"` is the one spelling correct against BOTH the pre-0.7.119
    wheels (fastembed extra-only) and every wheel after (`daemon = []`, an empty
    alias uv resolves silently). It stays, permanently, on every such command.

    Scope is an explicit allowlist of SHIPPED surfaces. `docs/superpowers/` and
    `.superpowers/` are excluded: they are historical plans, specs and agent
    working notes recording what was true at the time, not instructions anyone
    runs today. `--index` is required in the match, which excludes local-source
    reinstalls like `uv tool install --force ".[daemon]"`.
    """
    shipped = [
        *(_ROOT / "plugin").rglob("*.md"),
        *(_ROOT / "plugin").rglob("*.ps1"),
        *(p for p in (_ROOT / "docs").glob("*.md")),
        *(_ROOT.glob("*.md")),
    ]
    offenders = []
    for path in sorted(set(shipped)):
        for n, line in enumerate(path.read_text().splitlines(), 1):
            if all(tok in line for tok in ("uv tool install", "--force", "--index")):
                if "mcpbrain[daemon]" not in line:
                    offenders.append(f"{path.relative_to(_ROOT)}:{n}: {line.strip()}")
    assert not offenders, (
        "fresh-install command(s) missing the [daemon] alias:\n  " + "\n  ".join(offenders))
