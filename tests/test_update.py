"""update.py tests — index-based update path (git-pull model retired)."""
import mcpbrain.update as upd


def test_resolve_uv_prefers_path_then_local_bin(monkeypatch, tmp_path):
    monkeypatch.setattr(upd.shutil, "which", lambda n: None)
    suffix = ".exe" if upd.os.name == "nt" else ""
    fake = tmp_path / ".local" / "bin" / f"uv{suffix}"
    fake.parent.mkdir(parents=True)
    fake.write_text("")
    monkeypatch.setattr(upd.Path, "home", classmethod(lambda cls: tmp_path))
    assert upd._resolve_uv() == str(fake)


def test_resolve_uv_falls_back_to_bare_name(monkeypatch):
    monkeypatch.setattr(upd.shutil, "which", lambda n: None)
    monkeypatch.setattr(upd.Path, "home",
                        classmethod(lambda cls: __import__("pathlib").Path("/nonexistent")))
    assert upd._resolve_uv() == "uv"


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
    assert any("uv" in str(tok) for tok in uv_cmd) and "--upgrade" in uv_cmd
