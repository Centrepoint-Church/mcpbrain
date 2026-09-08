"""Nothing shipped may name a tenant except tenant.json.

The Centrepoint-specific surface was originally four values and a set of install
docs, all baked into the build — and it re-accumulated over time because nothing
noticed. This test is what keeps the repo tenant-neutral as it evolves.

Scope note: it checks tenant IDENTIFIERS (org names and infrastructure ids), NOT
staff names. A permanent test enumerating real people's surnames in a public repo,
in order to assert their absence, reintroduces the problem it exists to solve. The
prompt carries a comment requiring fictional examples instead.
"""
import json
import re
from pathlib import Path

_ROOT = Path(__file__).parent.parent
_PROFILE = json.loads((_ROOT / "mcpbrain" / "tenant.json").read_text())

# Case-sensitive for the acronyms: a lowercase `acc` is a perfectly ordinary
# accumulator variable, while a standalone uppercase ACC is an org name.
_PATTERNS = [
    re.compile(r"centrepoint", re.IGNORECASE),
    re.compile(r"courageous", re.IGNORECASE),
    re.compile(r"\bACCI?\b"),
    re.compile(re.escape(_PROFILE["fleet_folder_id"])),
    re.compile(re.escape(_PROFILE["escrow_folder_id"])),
]

# tenant.json is the ONE place a tenant may be named FREELY.
#
# The install surface is exempt for a different reason: these files carry runnable
# commands that MUST name the tenant's marketplace and index, so "absence" is the
# wrong test for them. tenant.check_offline covers them with a STRONGER one — they
# must AGREE with tenant.json — so exempting them here loses nothing.
_ALLOWED = {
    Path("mcpbrain/tenant.json"),
    Path("plugin/.claude-plugin/marketplace.json"),
    Path("plugin/.claude-plugin/plugin.json"),
    Path("plugin/scripts/install.ps1"),
    Path("plugin/commands/install.md"),
    Path("plugin/INSTALL.md"),
}
_ROOTS = ("mcpbrain", "plugin")
_SUFFIXES = {".py", ".md", ".json", ".html", ".ps1", ".txt", ".toml"}


def _shipped_files():
    for root in _ROOTS:
        for p in sorted((_ROOT / root).rglob("*")):
            if not p.is_file() or p.suffix not in _SUFFIXES:
                continue
            if "__pycache__" in p.parts:
                continue
            rel = p.relative_to(_ROOT)
            if rel in _ALLOWED:
                continue
            yield p, rel


def test_no_shipped_file_names_a_tenant_except_tenant_json():
    offenders = []
    for path, rel in _shipped_files():
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for pat in _PATTERNS:
                if pat.search(line):
                    offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
                    break
    assert not offenders, (
        "shipped file(s) name a tenant outside mcpbrain/tenant.json — move the value "
        "into the profile and read it from mcpbrain.tenant:\n  " + "\n  ".join(offenders))


def test_the_example_template_holds_no_real_values():
    text = (_ROOT / "mcpbrain" / "tenant.example.json").read_text()
    for pat in _PATTERNS:
        assert not pat.search(text), (
            "tenant.example.json carries a real tenant value; it must ship only "
            "placeholders a fork is forced to replace")
