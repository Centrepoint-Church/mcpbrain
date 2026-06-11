import json

import pytest

from mcpbrain.wizard import register


def test_config_path_darwin(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = register.claude_desktop_config_path("darwin")
    assert str(p).endswith(
        "Library/Application Support/Claude/claude_desktop_config.json"
    )


def test_config_path_win32(monkeypatch, tmp_path):
    appdata = tmp_path / "Roaming"
    monkeypatch.setenv("APPDATA", str(appdata))
    p = register.claude_desktop_config_path("win32")
    assert p == appdata / "Claude" / "claude_desktop_config.json"


def test_config_path_linux(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = register.claude_desktop_config_path("linux")
    assert p == tmp_path / ".config" / "Claude" / "claude_desktop_config.json"


def test_register_into_missing_file(tmp_path):
    cfg = tmp_path / "nested" / "cfg.json"
    out = register.register_mcpbrain(
        config_path=cfg,
        mcpbrain_bin="/usr/local/bin/mcpbrain",
        mcpbrain_home=tmp_path / "home",
    )
    assert out == cfg
    assert cfg.exists()
    data = json.loads(cfg.read_text())
    entry = data["mcpServers"]["mcpbrain"]
    assert entry["command"] == "/usr/local/bin/mcpbrain"
    assert entry["args"] == ["mcp-server"]
    assert entry["env"]["MCPBRAIN_HOME"] == str(tmp_path / "home")
    assert "MCPBRAIN_EMBEDDER" not in entry["env"]


def test_register_preserves_other_server(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    register.register_mcpbrain(
        config_path=cfg, mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home=tmp_path
    )
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["mcpbrain"]["command"] == "/usr/local/bin/mcpbrain"


def test_register_idempotent(tmp_path):
    cfg = tmp_path / "cfg.json"
    register.register_mcpbrain(
        config_path=cfg,
        mcpbrain_bin="/usr/local/bin/mcpbrain",
        mcpbrain_home=tmp_path / "h",
    )
    first = cfg.read_bytes()
    register.register_mcpbrain(
        config_path=cfg,
        mcpbrain_bin="/usr/local/bin/mcpbrain",
        mcpbrain_home=tmp_path / "h",
    )
    second = cfg.read_bytes()
    assert first == second


def test_register_rerunnable_update(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text(json.dumps({"mcpServers": {"other": {"command": "x"}}}))
    register.register_mcpbrain(
        config_path=cfg, mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home=tmp_path / "h1"
    )
    register.register_mcpbrain(
        config_path=cfg, mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home=tmp_path / "h2"
    )
    data = json.loads(cfg.read_text())
    assert data["mcpServers"]["mcpbrain"]["env"]["MCPBRAIN_HOME"] == str(
        tmp_path / "h2"
    )
    assert data["mcpServers"]["other"] == {"command": "x"}


def test_register_malformed_config_raises_no_clobber(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text("{not json")
    with pytest.raises(ValueError) as exc:
        register.register_mcpbrain(
            config_path=cfg, mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home=tmp_path
        )
    assert str(cfg) in str(exc.value)
    assert cfg.read_text() == "{not json"


# Repurposed from test_register_omits_env_when_no_vars: the new form ALWAYS
# sets MCPBRAIN_HOME.
def test_register_always_sets_home_env(tmp_path):
    cfg = tmp_path / "cfg.json"
    register.register_mcpbrain(
        config_path=cfg, mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home=tmp_path / "h"
    )
    entry = json.loads(cfg.read_text())["mcpServers"]["mcpbrain"]
    assert entry["env"]["MCPBRAIN_HOME"] == str(tmp_path / "h")
    assert "MCPBRAIN_EMBEDDER" not in entry["env"]


# Repurposed from test_register_python_path_coerced_to_str: the bin Path arg
# is coerced to str.
def test_register_bin_path_coerced_to_str(tmp_path):
    from pathlib import Path

    cfg = tmp_path / "cfg.json"
    register.register_mcpbrain(
        config_path=cfg,
        mcpbrain_bin=Path("/usr/local/bin/mcpbrain"),
        mcpbrain_home=tmp_path,
    )
    entry = json.loads(cfg.read_text())["mcpServers"]["mcpbrain"]
    assert entry["command"] == "/usr/local/bin/mcpbrain"
    assert isinstance(entry["command"], str)


# Fix 2 / Fix 4 — valid JSON that isn't an object raises ValueError without clobbering
def test_register_non_object_config_raises_no_clobber(tmp_path):
    cfg = tmp_path / "cfg.json"
    cfg.write_text("[1, 2, 3]")
    with pytest.raises(ValueError) as exc:
        register.register_mcpbrain(
            config_path=cfg, mcpbrain_bin="/usr/local/bin/mcpbrain", mcpbrain_home=tmp_path
        )
    assert str(cfg) in str(exc.value)
    assert cfg.read_text() == "[1, 2, 3]"


# Fix 5 — darwin path test uses full Path equality (not endswith)
def test_config_path_darwin_full_equality(monkeypatch, tmp_path):
    monkeypatch.setenv("HOME", str(tmp_path))
    p = register.claude_desktop_config_path("darwin")
    assert p == tmp_path / "Library" / "Application Support" / "Claude" / "claude_desktop_config.json"


def test_registers_mcpbrain_console_command(tmp_path, monkeypatch):
    import mcpbrain.wizard.register as reg
    import json
    cfgp = tmp_path / "claude_desktop_config.json"
    cfgp.write_text('{"mcpServers": {"other": {"command": "x"}}}')
    monkeypatch.setattr(reg, "claude_desktop_config_path", lambda platform=None: str(cfgp))
    monkeypatch.setattr(reg.shutil, "which", lambda n: "/usr/local/bin/mcpbrain")
    reg.register_mcpbrain(mcpbrain_home=str(tmp_path))
    cfg = json.loads(cfgp.read_text())
    assert cfg["mcpServers"]["other"]["command"] == "x"            # preserved
    e = cfg["mcpServers"]["mcpbrain"]
    assert e["command"] == "/usr/local/bin/mcpbrain" and e["args"] == ["mcp-server"]
    assert e["env"]["MCPBRAIN_HOME"] == str(tmp_path)


def test_register_raises_when_bin_unresolved(tmp_path, monkeypatch):
    cfg = tmp_path / "cfg.json"
    # Force both resolution paths to fail: sys.argv[0] isn't "mcpbrain", and
    # the PATH lookup returns None. The actionable message must mention PATH.
    monkeypatch.setattr(register.sys, "argv", ["pytest"])
    monkeypatch.setattr(register.shutil, "which", lambda n: None)
    with pytest.raises(RuntimeError, match="PATH"):
        register.register_mcpbrain(config_path=cfg, mcpbrain_home=tmp_path)


def test_register_main_dispatches(tmp_path, monkeypatch):
    cfgp = tmp_path / "claude_desktop_config.json"
    monkeypatch.setattr(
        register, "claude_desktop_config_path", lambda platform=None: str(cfgp)
    )
    rc = register.main(["--home", str(tmp_path), "--mcpbrain-bin", "/x/mcpbrain"])
    assert rc == 0
    entry = json.loads(cfgp.read_text())["mcpServers"]["mcpbrain"]
    assert entry["command"] == "/x/mcpbrain"
    assert entry["args"] == ["mcp-server"]
    assert entry["env"]["MCPBRAIN_HOME"] == str(tmp_path)
