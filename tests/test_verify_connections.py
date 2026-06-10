import json
from mcpbrain import probes


def test_verify_writes_cache(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text(json.dumps({
        "owner_name": "S", "owner_email": "s@x", "orgs": [{"name": "O"}],
        "clickup_api_key": "pk_x", "clickup_list_id": "L1", "timezone": "UTC"}))
    home = str(tmp_path)
    monkeypatch.setattr(probes, "_verify_clickup", lambda h: {"state": "ok", "detail": "verified", "last_verified": "t"})
    monkeypatch.setattr(probes, "_verify_google", lambda h: {"state": "ok", "detail": "token ok", "last_verified": "t"})
    probes.verify_connections(home, store=None)
    cache = json.loads((tmp_path / "connections.json").read_text())
    assert cache["clickup"]["detail"] == "verified"
    assert cache["google"]["detail"] == "token ok"
