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

    Asserts the API surface, not the version number, so this stays meaningful
    across the 1.x -> 2.x port (only the expected-attribute list changes).
    """
    from mcp.server import Server

    server = Server("contract-probe")
    missing = [
        attr for attr in ("list_resources", "read_resource", "list_tools", "call_tool")
        if not hasattr(server, attr)
    ]
    assert not missing, (
        f"mcp.server.Server is missing {missing} — mcp_server.py's registration "
        "layer will raise AttributeError at startup"
    )


def test_call_tool_validates_input_by_default():
    """We rely on the SDK validating arguments against inputSchema.

    mcp 1.x `call_tool(validate_input=True)` is the default and mcp_server.py uses
    the bare decorator, so all 26 tools get free jsonschema validation. mcp 2.x's
    low-level server validates NOTHING. If this assertion ever fails, validation
    must be re-implemented in mcpbrain before the tool surface is trusted.
    """
    import inspect

    from mcp.server import Server

    sig = inspect.signature(Server.call_tool)
    param = sig.parameters.get("validate_input")
    assert param is not None, "Server.call_tool no longer takes validate_input"
    assert param.default is True, (
        "Server.call_tool no longer validates input by default — mcpbrain must "
        "validate arguments against inputSchema itself"
    )
