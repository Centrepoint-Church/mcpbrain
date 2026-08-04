import ast
import sys
import importlib
from pathlib import Path

import mcpbrain.mcp_server
import mcpbrain.tools

# Symbols that pull the native embedder (fastembed/onnxruntime) into the MCP bridge
# process. brain_search must stay routed through the daemon (ControlClient), so
# neither symbol may appear anywhere in the bridge's own source — not as an import,
# and not as a bare reference (e.g. a call re-added inside main()).
_NATIVE_SYMBOLS = {"get_embedder", "hybrid_search"}

# BOTH halves of the MCP bridge. This used to be mcp_server alone, which was the
# whole file; the tool factories then moved to tools.py, and `make_brain_search`
# -- the handler this guard exists for -- moved with them. Scanning only
# mcp_server would have left the guard passing while guarding nothing: an
# in-process embedder re-added to make_brain_search satisfied every gate in the
# suite. Add a module here if the bridge ever splits further.
_BRIDGE_MODULES = (mcpbrain.mcp_server, mcpbrain.tools)


def test_importing_the_mcp_bridge_pulls_no_native_deps():
    for name in [m for m in sys.modules if m == "fastembed" or m.startswith("onnxruntime")]:
        del sys.modules[name]
    # Both must be dropped before the re-import. Popping only mcp_server left
    # tools.py cached, so its module-level imports never re-ran and a native
    # import added there was invisible to this test.
    for mod in _BRIDGE_MODULES:
        sys.modules.pop(mod.__name__, None)
    for mod in _BRIDGE_MODULES:
        importlib.import_module(mod.__name__)
    leaked = [m for m in sys.modules if m == "fastembed" or m.startswith("onnxruntime")]
    assert leaked == [], f"the MCP bridge pulled native deps: {leaked}"


def test_mcp_bridge_source_never_imports_or_references_native_embedder():
    """Regression guard: get_embedder()/hybrid_search only matter from main(), which
    the import test above never runs. Parse the source with ast so a re-added
    `from mcpbrain.embed import get_embedder` (or a bare `get_embedder(...)` call
    inside main()) fails this test even though a plain module import stays clean.

    Covers mcp_server.py AND tools.py: `from mcpbrain.embed import ...` has root
    module `mcpbrain`, which tools.py's own import guard in
    test_tool_registry.py deliberately allows, so this is the only check that
    catches it there.
    """
    for mod in _BRIDGE_MODULES:
        name = Path(mod.__file__).name
        tree = ast.parse(Path(mod.__file__).read_text())

        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                for alias in node.names:
                    imported.add(alias.name)
        leaked_imports = imported & _NATIVE_SYMBOLS
        assert not leaked_imports, (
            f"{name} imports native-embedder symbols: {leaked_imports}"
        )

        referenced = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name):
                referenced.add(node.id)
            elif isinstance(node, ast.Attribute):
                referenced.add(node.attr)
        leaked_refs = referenced & _NATIVE_SYMBOLS
        assert not leaked_refs, (
            f"{name} references native-embedder symbols: {leaked_refs}"
        )
