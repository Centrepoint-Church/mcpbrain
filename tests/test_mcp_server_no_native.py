import ast
import sys
import importlib
from pathlib import Path

import mcpbrain.mcp_server

# Symbols that pull the native embedder (fastembed/onnxruntime) into the MCP bridge
# process. brain_search must stay routed through the daemon (ControlClient), so
# neither symbol may appear anywhere in mcp_server.py — not as an import, and not
# as a bare reference (e.g. a call re-added inside main()).
_NATIVE_SYMBOLS = {"get_embedder", "hybrid_search"}


def test_importing_mcp_server_pulls_no_native_deps():
    for name in [m for m in sys.modules if m == "fastembed" or m.startswith("onnxruntime")]:
        del sys.modules[name]
    sys.modules.pop("mcpbrain.mcp_server", None)
    importlib.import_module("mcpbrain.mcp_server")
    leaked = [m for m in sys.modules if m == "fastembed" or m.startswith("onnxruntime")]
    assert leaked == [], f"mcp_server pulled native deps: {leaked}"


def test_mcp_server_source_never_imports_or_references_native_embedder():
    """Regression guard: get_embedder()/hybrid_search only matter from main(), which
    the import test above never runs. Parse the source with ast so a re-added
    `from mcpbrain.embed import get_embedder` (or a bare `get_embedder(...)` call
    inside main()) fails this test even though a plain module import stays clean."""
    source = Path(mcpbrain.mcp_server.__file__).read_text()
    tree = ast.parse(source)

    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                imported.add(alias.name)
    leaked_imports = imported & _NATIVE_SYMBOLS
    assert not leaked_imports, (
        f"mcp_server.py imports native-embedder symbols: {leaked_imports}"
    )

    referenced = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            referenced.add(node.id)
        elif isinstance(node, ast.Attribute):
            referenced.add(node.attr)
    leaked_refs = referenced & _NATIVE_SYMBOLS
    assert not leaked_refs, (
        f"mcp_server.py references native-embedder symbols: {leaked_refs}"
    )
