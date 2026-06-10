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
    assert uv_cmd[0] == "uv" and "tool" in uv_cmd and "install" in uv_cmd
    assert any("mcpbrain=" in c for c in uv_cmd)        # --index mcpbrain=<url>
    assert "mcpbrain" in uv_cmd and "--upgrade" in uv_cmd
    assert calls["restart"] == 1
