"""pymupdf is imported by its own name and its range is bounded.

pymupdf 1.28 warns that its legacy module alias (the pre-rename name) is
deprecated and will be removed, and the fleet re-resolves dependencies on every
daily auto-update. An unbounded `pymupdf>=` would let a release that actually
removes the alias (or changes extraction output) reach every install untested,
which is the shape of the 0.7.112 `mcp` outage.
"""
import re
import tomllib
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).parent.parent
_OLD = "fi" + "tz"  # spelled split so this guard does not match itself
_LEGACY = re.compile(rf"^\s*(import {_OLD}\b|from {_OLD}\b)|\b{_OLD}\.", re.M)


def _pymupdf_req() -> Requirement:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for raw in data["project"]["dependencies"]:
        req = Requirement(raw)
        if req.name == "pymupdf":
            return req
    raise AssertionError("pyproject.toml declares no pymupdf dependency")


def test_no_code_uses_the_deprecated_legacy_module_name():
    hits = []
    for top in ("mcpbrain", "bin", "tests"):
        for p in (ROOT / top).rglob("*.py"):
            for n, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
                if _LEGACY.search(line):
                    hits.append(f"{p.relative_to(ROOT)}:{n}: {line.strip()}")
    assert not hits, f"use `import pymupdf`, not {_OLD}:\n" + "\n".join(hits)


def test_pymupdf_range_is_bounded_to_the_tested_minors():
    spec = _pymupdf_req().specifier
    assert Version("1.27.2.3") in spec          # the lock
    assert Version("1.28.2") in spec            # what the fleet resolves
    assert Version("1.26.0") not in spec
    assert Version("1.29.0") not in spec        # untested: bump deliberately


def test_installed_pymupdf_satisfies_declared_range():
    assert Version(version("pymupdf")) in _pymupdf_req().specifier
