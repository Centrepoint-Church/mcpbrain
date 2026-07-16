from mcpbrain import update


def test_latest_version_parses_pep503_index(monkeypatch):
    html = ('<!DOCTYPE html><html><body>'
            '<a href="mcpbrain-0.2.0-py3-none-any.whl">mcpbrain-0.2.0-py3-none-any.whl</a>'
            '<a href="mcpbrain-0.10.1-py3-none-any.whl">x</a>'
            '<a href="mcpbrain-0.9.0-py3-none-any.whl">x</a>'
            '</body></html>')
    monkeypatch.setattr(update, "_fetch", lambda url: html)
    assert update._latest_version("https://x/simple/") == "0.10.1"  # numeric, not lexical


def test_should_update_true_when_behind():
    assert update._should_update("0.2.0", "0.10.1") is True
    assert update._should_update("0.10.1", "0.10.1") is False
    assert update._should_update("0.11.0", "0.10.1") is False


def test_update_from_index_runs_uv_then_restart(monkeypatch):
    calls = {"run": [], "restart": 0}
    monkeypatch.setattr(update, "_run", lambda cmd: (calls["run"].append(cmd), ("", 0))[1])
    monkeypatch.setattr(update, "_restart_agent", lambda: calls.__setitem__("restart", 1))
    rc = update.update_from_index("https://org.github.io/mcpbrain-dist/simple/")
    assert rc == 0
    uv_cmd = calls["run"][0]
    assert "uv" in uv_cmd[0] and "tool" in uv_cmd and "install" in uv_cmd
    assert any("mcpbrain=" in c for c in uv_cmd)        # --index mcpbrain=<url>
    # Install spec must carry the [daemon] extra so the daemon keeps fastembed
    # (onnxruntime) after an auto-update — plain "mcpbrain" would silently drop it.
    assert "mcpbrain[daemon]" in uv_cmd and "--upgrade" in uv_cmd
    # --reinstall-package takes the bare package name (extras aren't a separate
    # installed package) — it must stay "mcpbrain", not the extras spec.
    assert uv_cmd[uv_cmd.index("--reinstall-package") + 1] == "mcpbrain"
    # Pin the interpreter so uv provisions Python 3.12 (mcpbrain requires >=3.12);
    # without this the install fails on a machine whose default Python is <3.12.
    assert "--python" in uv_cmd and "3.12" in uv_cmd
    assert calls["restart"] == 1


def test_main_warns_and_exits_on_change_me_url(monkeypatch, capsys):
    """main() must return 1 and print to stderr when index URL is still the placeholder."""
    monkeypatch.setattr(update, "_index_url", lambda: "https://CHANGE-ME.github.io/mcpbrain-dist/simple/")

    def _boom(*a, **kw):
        raise AssertionError("_latest_version must not be called with unconfigured URL")

    def _boom2(*a, **kw):
        raise AssertionError("update_from_index must not be called with unconfigured URL")

    monkeypatch.setattr(update, "_latest_version", _boom)
    monkeypatch.setattr(update, "update_from_index", _boom2)

    rc = update.main([])
    assert rc == 1
    captured = capsys.readouterr()
    assert "CHANGE-ME" in captured.err or "not configured" in captured.err.lower()
