"""nameparser's range stops below 2, where HumanName was removed.

nameparser 3.0 is a rewrite: `from nameparser import HumanName` raises
ImportError, and graph_write imports it at module load, so the daemon, the CLI
and the MCP server all fail to start. The fleet re-resolves dependencies on
every daily auto-update, so an unbounded `nameparser>=` reached the published
index on 2026-11-09 (0.7.145 resolved nameparser 3.0.1). Same shape as the
0.7.112 `mcp` outage.
"""
import tomllib
from importlib.metadata import version
from pathlib import Path

from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).parent.parent


def _nameparser_req() -> Requirement:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for raw in data["project"]["dependencies"]:
        req = Requirement(raw)
        if req.name == "nameparser":
            return req
    raise AssertionError("pyproject.toml declares no nameparser dependency")


def test_nameparser_range_excludes_the_rewrite_without_humanname():
    spec = _nameparser_req().specifier
    assert Version("1.1.3") in spec             # the lock, and what the fleet ran
    assert Version("2.0.0") not in spec
    assert Version("3.0.1") not in spec         # removed HumanName


def test_installed_nameparser_satisfies_declared_range_and_has_humanname():
    assert Version(version("nameparser")) in _nameparser_req().specifier
    from nameparser import HumanName  # noqa: F401
