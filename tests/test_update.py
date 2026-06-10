"""update.py tests — index-based update path (git-pull model retired)."""
import mcpbrain.update as upd


def test_update_main_up_to_date(monkeypatch):
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.3.0")
    monkeypatch.setattr(upd, "_latest_version", lambda url: "0.3.0")
    monkeypatch.setattr(upd, "update_from_index", lambda url: (_ for _ in ()).throw(AssertionError("must not update")))
    rc = upd.main([])
    assert rc == 0


def test_update_main_triggers_when_behind(monkeypatch):
    calls = {"run": [], "restart": 0}
    monkeypatch.setattr(upd, "_index_url", lambda: "https://x/simple/")
    monkeypatch.setattr(upd, "_installed_version", lambda: "0.2.0")
    monkeypatch.setattr(upd, "_latest_version", lambda url: "0.3.0")
    monkeypatch.setattr(upd, "_run", lambda cmd: (calls["run"].append(cmd), ("", 0))[1])
    monkeypatch.setattr(upd, "_restart_agent", lambda: calls.__setitem__("restart", 1))
    rc = upd.main([])
    assert rc == 0
    assert calls["restart"] == 1
    uv_cmd = calls["run"][0]
    assert "uv" in uv_cmd and "--upgrade" in uv_cmd
