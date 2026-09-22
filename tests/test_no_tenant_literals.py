"""Nothing shipped may name a tenant except tenant.json.

The Centrepoint-specific surface was originally four values and a set of install
docs, all baked into the build — and it re-accumulated over time because nothing
noticed. This test is what keeps the repo tenant-neutral as it evolves.

This guard originally scanned only mcpbrain/ and plugin/ and only checked ORG
identifiers, on the theory that tests/ was "fixtures and history" and a permanent
test enumerating real people's surnames in a public repo, in order to assert their
absence, would just reintroduce the problem it exists to solve. Both halves of that
theory turned out to be wrong: a real staff surname ("Okafor") sat in tests/ for
months, in 10+ files and this repo's own CLAUDE.md, uncaught by either half.

- `tests/` is now a scanned root (see `_ROOTS` below) for the same org-identifier
  patterns as mcpbrain/ and plugin/.
- The person-name half is real, but the fix is the same one this repo already uses
  for the gold eval sets (tests/eval/golden_retrieval_set*.yaml): the forbidden list
  is TENANT DATA, not something this public test can enumerate. It lives in the
  private mcpbrain-tenant repo as `tenant_people.json`, is copied in by
  `python bin/tenant.py use ../mcpbrain-tenant` (see bin/tenant.py's `_OPTIONAL`),
  and is gitignored here. `test_no_person_name_leaks_when_tenant_people_file_present`
  below reads it IF PRESENT and skips honestly when it is absent — a fork (or a
  fresh checkout that hasn't run `tenant.py use`) gets a clean skip, not a failure,
  and never sees a list of real names.
"""
import json
import re
from pathlib import Path

import pytest

_ROOT = Path(__file__).parent.parent
_PROFILE = json.loads((_ROOT / "mcpbrain" / "tenant.json").read_text())
_PEOPLE_FILE = _ROOT / "mcpbrain" / "tenant_people.json"

# Case-sensitive for the acronyms: a lowercase `acc` is a perfectly ordinary
# accumulator variable, while a standalone uppercase ACC is an org name.
_PATTERNS = [
    re.compile(r"centrepoint", re.IGNORECASE),
    re.compile(r"courageous", re.IGNORECASE),
    re.compile(r"\bACCI?\b"),
]
# fleet_folder_id/escrow_folder_id are OPTIONAL — tenant.example.json and
# docs/FORKING.md both tell a fork it may leave them blank to disable
# fleet/backup. re.escape("") compiles to an empty pattern, and an empty
# regex's .search() matches every string at every position, so only add
# these patterns when the tenant profile actually has a value to guard.
for _field in ("fleet_folder_id", "escrow_folder_id"):
    _value = _PROFILE.get(_field)
    if _value:
        _PATTERNS.append(re.compile(re.escape(_value)))

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
    # This file's own docstring/comments describe what the patterns below are FOR
    # ("centrepoint", "courageous") — it cannot explain the guard without naming
    # the thing the guard forbids.
    Path("tests/test_no_tenant_literals.py"),
    # test_tenant.py tests tenant.py's behaviour against the REAL bundled
    # mcpbrain/tenant.json (the one file this guard already lets name a tenant
    # freely) — e.g. that the bundled tenant_id is "centrepoint" and the bundled
    # marketplace_slug is "Centrepoint-Church/mcpbrain-plugin". Renaming the
    # literals here would make the test assert something other than the real,
    # shipped profile.
    Path("tests/test_tenant.py"),
}
_ROOTS = ("mcpbrain", "plugin", "tests")
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


def test_no_person_name_leaks_when_tenant_people_file_present():
    """Scan mcpbrain/, plugin/ and tests/ for real people's names, using a
    forbidden-names list that is itself tenant data (see the module docstring).

    Skips honestly when mcpbrain/tenant_people.json is absent — the normal state
    for a fresh checkout or a fork, which has never run
    `python bin/tenant.py use ../mcpbrain-tenant` and has no such list to check
    against. This is the same shape as the gold-eval-set tests: an optional,
    gitignored, tenant-supplied file gates an otherwise-skipped check.
    """
    if not _PEOPLE_FILE.is_file():
        pytest.skip("no mcpbrain/tenant_people.json — nothing to check (fork/fresh checkout)")

    names = json.loads(_PEOPLE_FILE.read_text())
    assert isinstance(names, list) and names, (
        "tenant_people.json must be a non-empty JSON list of names to guard")
    patterns = [re.compile(r"\b" + re.escape(name) + r"\b", re.IGNORECASE)
                for name in names]

    people_rel = _PEOPLE_FILE.relative_to(_ROOT)
    offenders = []
    for path, rel in _shipped_files():
        if rel == people_rel:
            continue  # the forbidden-names list itself necessarily names them
        try:
            text = path.read_text()
        except UnicodeDecodeError:
            continue
        for n, line in enumerate(text.splitlines(), 1):
            for pat in patterns:
                if pat.search(line):
                    offenders.append(f"{rel}:{n}: {line.strip()[:100]}")
                    break
    assert not offenders, (
        "shipped file(s) name a real person listed in tenant_people.json — replace "
        "with this repo's fictional cast (see CLAUDE.md) rather than a real name:\n  "
        + "\n  ".join(offenders))


def test_blank_optional_fields_do_not_produce_an_always_matching_pattern():
    # A fork with fleet/backup disabled leaves fleet_folder_id/escrow_folder_id
    # blank (a documented, supported configuration — see tenant.example.json and
    # docs/FORKING.md). Guard that this never regresses into re.compile(re.escape(""))
    # being added to _PATTERNS, since an empty pattern's .search() matches every
    # line of every shipped file.
    blank_profile = {"fleet_folder_id": "", "escrow_folder_id": ""}
    patterns = []
    for field in ("fleet_folder_id", "escrow_folder_id"):
        value = blank_profile.get(field)
        if value:
            patterns.append(re.compile(re.escape(value)))
    assert patterns == []
