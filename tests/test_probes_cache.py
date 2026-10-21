import json
from datetime import datetime, timedelta, timezone
from mcpbrain import probes

def _home(tmp_path, cfg=None):
    (tmp_path / "config.json").write_text(json.dumps(cfg or {})); return str(tmp_path)

def test_claude_goes_stale_past_window(tmp_path):
    home = _home(tmp_path)
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    (tmp_path / "mcp_heartbeat.json").write_text(json.dumps({"last_seen": old}))
    assert probes.probe_claude(home)["state"] == "needs_action"

def test_claude_ok_within_window(tmp_path):
    home = _home(tmp_path)
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    (tmp_path / "mcp_heartbeat.json").write_text(json.dumps({"last_seen": fresh}))
    assert probes.probe_claude(home)["state"] == "ok"

def test_all_connections_prefers_cache(tmp_path):
    # A live connection that is not not_started (claude, via a fresh heartbeat)
    # lets the verified cache result win over the cheap live probe.
    home = _home(tmp_path, {})
    fresh = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
    (tmp_path / "mcp_heartbeat.json").write_text(json.dumps({"last_seen": fresh}))
    cache = {"claude": {"state": "needs_action", "detail": "cached override",
                        "last_verified": "2026-06-10T00:00:00+00:00"}}
    (tmp_path / "connections.json").write_text(json.dumps(cache))
    conns = probes.all_connections(home, store=None)
    assert conns["claude"]["state"] == "needs_action" and conns["claude"]["detail"] == "cached override"

def test_removed_connection_overrides_stale_cache(tmp_path):
    # No heartbeat → claude live probe is not_started; a stale cached "ok" must
    # NOT win (status flips to not_started immediately on removal).
    home = _home(tmp_path, {})
    cache = {"claude": {"state": "ok", "detail": "Verified", "last_verified": "t"}}
    (tmp_path / "connections.json").write_text(json.dumps(cache))
    assert probes.all_connections(home)["claude"]["state"] == "not_started"


def test_refresh_connection_cache_clears_stale_expired(tmp_path, monkeypatch):
    # Repro of the re-auth bug: connections.json still says google expired, but a
    # fresh token makes the live probe 'ok'. all_connections lets the stale cache
    # win — until refresh_connection_cache() brings it current.
    home = _home(tmp_path, {})
    stale = {"google": {"state": "needs_action", "detail": "Sign-in expired — reconnect",
                        "last_verified": None}}
    (tmp_path / "connections.json").write_text(json.dumps(stale))
    monkeypatch.setattr(probes, "probe_google",
                        lambda h: {"state": "ok", "detail": "Connected", "last_verified": "t"})
    # Before refresh: stale cache masks the recovered connection.
    assert probes.all_connections(home)["google"]["state"] == "needs_action"
    # Refresh writes the live 'ok' into the cache...
    out = probes.refresh_connection_cache(home, "google")
    assert out["state"] == "ok"
    assert json.loads((tmp_path / "connections.json").read_text())["google"]["state"] == "ok"
    # ...so the status now reflects reality immediately.
    assert probes.all_connections(home)["google"]["state"] == "ok"


def test_refresh_connection_cache_merges_not_clobbers(tmp_path, monkeypatch):
    # Refreshing one connection must not drop other cached entries.
    home = _home(tmp_path, {})
    (tmp_path / "connections.json").write_text(json.dumps(
        {"claude": {"state": "ok", "detail": "Verified", "last_verified": "t"}}))
    monkeypatch.setattr(probes, "probe_google",
                        lambda h: {"state": "ok", "detail": "Connected", "last_verified": "t"})
    probes.refresh_connection_cache(home, "google")
    cache = json.loads((tmp_path / "connections.json").read_text())
    assert cache["google"]["state"] == "ok" and cache["claude"]["detail"] == "Verified"


def test_refresh_connection_cache_unknown_name(tmp_path):
    assert probes.refresh_connection_cache(_home(tmp_path, {}), "nope") is None


# ---------- probe_backup ----------

import time  # noqa: E402

_BACKUP_CFG = {"backup": {"dest": "s3://some-bucket"}}


def _bstate(tmp_path, **kw):
    (tmp_path / "backup_state.json").write_text(json.dumps(kw))


def test_backup_no_backup_yet_without_state_file(tmp_path):
    home = _home(tmp_path, _BACKUP_CFG)
    (tmp_path / "snapshot.enc").write_bytes(b"data")  # stale leftover: ignored
    r = probes.probe_backup(home)
    assert r["state"] == "needs_action"
    assert "no backup yet" in r["detail"].lower()


def test_probe_uses_restore_point(tmp_path):
    """restore_point (capture time) is the signal, even when last_success is old."""
    home = _home(tmp_path, _BACKUP_CFG)
    _bstate(tmp_path, restore_point=time.time() - 2 * 3600,
            last_success=time.time() - 30 * 86400, consecutive_failures=0)
    r = probes.probe_backup(home)
    assert r["state"] == "ok"
    assert "restore point 2h ago" in r["detail"]


def test_probe_falls_back_to_last_success(tmp_path):
    home = _home(tmp_path, _BACKUP_CFG)
    _bstate(tmp_path, last_success=time.time() - 3600, consecutive_failures=0)
    assert probes.probe_backup(home)["state"] == "ok"


def test_backup_stale_restore_point_needs_action(tmp_path):
    home = _home(tmp_path, _BACKUP_CFG)
    _bstate(tmp_path, restore_point=time.time() - 3 * 86400, consecutive_failures=0)
    assert probes.probe_backup(home)["state"] == "needs_action"


def test_backup_needs_action_when_uploads_keep_failing(tmp_path):
    home = _home(tmp_path, _BACKUP_CFG)
    _bstate(tmp_path, restore_point=time.time() - 3600,
            last_success=time.time() - 30 * 3600, consecutive_failures=5,
            last_error="Broken pipe")
    r = probes.probe_backup(home)
    assert r["state"] == "needs_action"
    assert "fail" in r["detail"].lower()


def test_backup_one_recent_failure_is_tolerated(tmp_path):
    home = _home(tmp_path, _BACKUP_CFG)
    _bstate(tmp_path, restore_point=time.time() - 3600,
            last_success=time.time() - 3600, consecutive_failures=1)
    assert probes.probe_backup(home)["state"] == "ok"


def test_backup_needs_action_when_state_file_is_corrupt(tmp_path):
    home = _home(tmp_path, _BACKUP_CFG)
    (tmp_path / "backup_state.json").write_text("{not valid json")
    assert probes.probe_backup(home)["state"] == "needs_action"


def test_last_backup_success_none_when_state_file_is_corrupt(tmp_path):
    home = _home(tmp_path, _BACKUP_CFG)
    (tmp_path / "backup_state.json").write_text("{not valid json")
    assert probes.last_backup_success(home) is None


def test_last_backup_success_ignores_snapshot_file(tmp_path):
    home = _home(tmp_path, _BACKUP_CFG)
    (tmp_path / "snapshot.enc").write_bytes(b"data")
    assert probes.last_backup_success(home) is None
    _bstate(tmp_path, last_success=123.0)
    assert probes.last_backup_success(home) == 123.0
