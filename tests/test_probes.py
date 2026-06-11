"""Connection probes return {state, detail, last_verified} tri-states."""
import json
from datetime import datetime, timezone

from mcpbrain import probes


def _home(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return str(tmp_path)


def test_claude_not_started_when_no_heartbeat(tmp_path, monkeypatch):
    monkeypatch.setattr(probes, "_claude_registered", lambda: False)
    r = probes.probe_claude(_home(tmp_path, {}))
    assert r["state"] == "not_started"
    assert r["last_verified"] is None


def test_claude_ok_when_heartbeat_present(tmp_path):
    home = _home(tmp_path, {})
    (tmp_path / "mcp_heartbeat.json").write_text(
        json.dumps({"last_seen": datetime(2026, 6, 10, tzinfo=timezone.utc).isoformat()})
    )
    r = probes.probe_claude(home)
    assert r["state"] == "ok"
    assert r["last_verified"].startswith("2026-06-10")


def test_clickup_not_started_without_key(tmp_path):
    assert probes.probe_clickup(_home(tmp_path, {}))["state"] == "not_started"


def test_clickup_needs_action_with_key_but_no_list(tmp_path):
    home = _home(tmp_path, {"clickup_api_key": "pk_x"})
    assert probes.probe_clickup(home)["state"] == "needs_action"


def test_clickup_ok_with_key_list_and_tz(tmp_path):
    home = _home(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1",
                             "timezone": "Australia/Perth"})
    assert probes.probe_clickup(home)["state"] == "ok"


def test_clickup_needs_action_without_timezone(tmp_path):
    home = _home(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})
    assert probes.probe_clickup(home)["state"] == "needs_action"


def test_records_ok_when_git_repo(tmp_path):
    home = _home(tmp_path, {})
    from mcpbrain import records
    records.ensure_records_repo(str(tmp_path / "records"), git_name="t", git_email="t@t")
    assert probes.probe_records(home)["state"] == "ok"


def test_records_not_started_when_absent(tmp_path):
    assert probes.probe_records(_home(tmp_path, {}))["state"] == "not_started"


def test_all_connections_has_every_key(tmp_path):
    conns = probes.all_connections(_home(tmp_path, {}), store=None)
    assert set(conns) == {"google", "claude", "clickup", "backup", "records",
                          "enrichment", "memory-hooks"}
    for v in conns.values():
        assert set(v) == {"state", "detail", "last_verified"}
        assert v["state"] in {"not_started", "ok", "needs_action"}


# --- Task 7 additions ---
import json as _json
from mcpbrain import probes as _probes


def _home7(tmp_path, cfg):
    (tmp_path / "config.json").write_text(_json.dumps(cfg))
    return str(tmp_path)


def test_claude_not_registered(tmp_path, monkeypatch):
    monkeypatch.setattr(_probes, "_claude_registered", lambda: False)
    r = _probes.probe_claude(_home7(tmp_path, {}))
    assert r["state"] == "not_started" and "register" in r["detail"].lower()


def test_claude_registered_awaiting_restart(tmp_path, monkeypatch):
    monkeypatch.setattr(_probes, "_claude_registered", lambda: True)
    r = _probes.probe_claude(_home7(tmp_path, {}))  # no heartbeat file
    assert r["state"] == "needs_action" and "reopen" in r["detail"].lower()


def test_enrichment_states(tmp_path, monkeypatch):
    home = _home7(tmp_path, {})
    monkeypatch.setattr(_probes.skills, "enrichment_skill_present", lambda: False)
    assert _probes.probe_enrichment(home)["state"] == "not_started"
    monkeypatch.setattr(_probes.skills, "enrichment_skill_present", lambda: True)
    assert _probes.probe_enrichment(home)["state"] == "needs_action"
    inbox = tmp_path / "enrich_inbox"; inbox.mkdir()
    (inbox / "batch-1.json").write_text("{}")
    assert _probes.probe_enrichment(home)["state"] == "ok"


def test_memory_hooks_probe(tmp_path, monkeypatch):
    monkeypatch.setattr(_probes.hooks, "hooks_status", lambda: {"installed": True})
    assert _probes.probe_memory_hooks(_home7(tmp_path, {}))["state"] == "ok"
    monkeypatch.setattr(_probes.hooks, "hooks_status", lambda: {"installed": False})
    assert _probes.probe_memory_hooks(_home7(tmp_path, {}))["state"] == "not_started"


def test_all_connections_has_new_keys(tmp_path, monkeypatch):
    monkeypatch.setattr(_probes, "_claude_registered", lambda: False)
    monkeypatch.setattr(_probes.skills, "enrichment_skill_present", lambda: False)
    monkeypatch.setattr(_probes.hooks, "hooks_status", lambda: {"installed": False})
    conns = _probes.all_connections(_home7(tmp_path, {}))
    assert {"enrichment", "memory-hooks"} <= set(conns)
