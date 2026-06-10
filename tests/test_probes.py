"""Connection probes return {state, detail, last_verified} tri-states."""
import json
from datetime import datetime, timezone

from mcpbrain import probes


def _home(tmp_path, cfg):
    (tmp_path / "config.json").write_text(json.dumps(cfg))
    return str(tmp_path)


def test_claude_not_started_when_no_heartbeat(tmp_path):
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


def test_clickup_ok_with_key_and_list(tmp_path):
    home = _home(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})
    assert probes.probe_clickup(home)["state"] == "ok"


def test_records_ok_when_git_repo(tmp_path):
    home = _home(tmp_path, {})
    from mcpbrain import records
    records.ensure_records_repo(str(tmp_path / "records"), git_name="t", git_email="t@t")
    assert probes.probe_records(home)["state"] == "ok"


def test_records_not_started_when_absent(tmp_path):
    assert probes.probe_records(_home(tmp_path, {}))["state"] == "not_started"


def test_all_connections_has_every_key(tmp_path):
    conns = probes.all_connections(_home(tmp_path, {}), store=None)
    assert set(conns) == {"google", "claude", "clickup", "backup", "records"}
    for v in conns.values():
        assert set(v) == {"state", "detail", "last_verified"}
        assert v["state"] in {"not_started", "ok", "needs_action"}
