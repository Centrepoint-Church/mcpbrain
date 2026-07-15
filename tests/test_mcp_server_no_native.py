import sys
import importlib


def test_importing_mcp_server_pulls_no_native_deps():
    for name in [m for m in sys.modules if m == "fastembed" or m.startswith("onnxruntime")]:
        del sys.modules[name]
    sys.modules.pop("mcpbrain.mcp_server", None)
    importlib.import_module("mcpbrain.mcp_server")
    leaked = [m for m in sys.modules if m == "fastembed" or m.startswith("onnxruntime")]
    assert leaked == [], f"mcp_server pulled native deps: {leaked}"
