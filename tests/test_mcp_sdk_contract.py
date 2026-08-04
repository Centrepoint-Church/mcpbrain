"""Guards the mcp SDK contract mcp_server.py depends on.

Context: on 2026-08-04 every Claude Desktop connection crashed with
`AttributeError: 'Server' object has no attribute 'list_resources'` because an
unpinned `uv tool install` re-resolve picked up mcp 2.0.0, which deleted the 1.x
low-level decorator API. Tests never saw it: uv.lock pinned 1.27.2 while the
fleet auto-updates unpinned. These tests fail loudly on the next ceiling break
instead of surfacing as a 15-second subprocess timeout in production.
"""
import tomllib
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def _declared_mcp_requirement() -> Requirement:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    for raw in data["project"]["dependencies"]:
        req = Requirement(raw)
        if req.name == "mcp":
            return req
    pytest.fail("pyproject.toml declares no `mcp` dependency")


def test_installed_mcp_satisfies_declared_range():
    """The mcp we actually import must satisfy the range pyproject declares."""
    from importlib.metadata import version

    installed = Version(version("mcp"))
    req = _declared_mcp_requirement()
    assert installed in req.specifier, (
        f"installed mcp {installed} violates declared {req}; "
        "the SDK API mcp_server.py targets may not exist"
    )


def test_lowlevel_server_exposes_the_api_mcp_server_targets():
    """mcp_server.py's registration layer must actually be callable.

    Asserts the API surface, not the version number. Retargeted for the 2.x port:
    the four 1.x decorators became `on_*` constructor keyword arguments, so what
    build_server() needs to exist is now a signature, not an attribute.
    """
    import inspect

    from mcp.server import Server

    params = inspect.signature(Server.__init__).parameters
    missing = [
        kw for kw in ("on_list_resources", "on_read_resource", "on_list_tools", "on_call_tool")
        if kw not in params
    ]
    assert not missing, (
        f"mcp.server.Server no longer accepts {missing} — build_server() will fail"
    )


def test_mcpbrain_validates_tool_arguments_itself():
    """mcp 2.x's low-level server validates nothing; we must.

    Replaces the 1.x test_call_tool_validates_input_by_default guard (mcp 1.x's
    `call_tool(validate_input=True)` default gave all 26 tools free jsonschema
    validation; 2.x's low-level server has no such parameter and validates
    NOTHING). If this ever fails, all 26 tools are accepting unvalidated
    arguments.
    """
    from mcpbrain.mcp_server import _validate_tool_arguments

    with pytest.raises(ValueError, match="unit_id"):
        _validate_tool_arguments("brain_enrich_push", {})
