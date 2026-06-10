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
    # clickup IS configured (key+list+tz) so the live probe is not not_started;
    # the verified cache result should win over the cheap "ok".
    home = _home(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1", "timezone": "UTC"})
    cache = {"clickup": {"state": "needs_action", "detail": "key invalid", "last_verified": "2026-06-10T00:00:00+00:00"}}
    (tmp_path / "connections.json").write_text(json.dumps(cache))
    conns = probes.all_connections(home, store=None)
    assert conns["clickup"]["state"] == "needs_action" and conns["clickup"]["detail"] == "key invalid"

def test_removed_connection_overrides_stale_cache(tmp_path):
    # No clickup key configured → live probe is not_started; a stale cached "ok"
    # must NOT win (status flips to not_started immediately on removal).
    home = _home(tmp_path, {})
    cache = {"clickup": {"state": "ok", "detail": "Verified", "last_verified": "t"}}
    (tmp_path / "connections.json").write_text(json.dumps(cache))
    assert probes.all_connections(home)["clickup"]["state"] == "not_started"

def test_clickup_needs_tz(tmp_path):
    home = _home(tmp_path, {"clickup_api_key": "pk_x", "clickup_list_id": "L1"})  # no timezone
    assert probes.probe_clickup(home)["state"] == "needs_action"


# ---------- probe_backup staleness / empty-file tests ----------

_BACKUP_CFG = {"backup": {"dest": "s3://some-bucket"}}


def test_backup_ok_with_fresh_nonempty_snapshot(tmp_path):
    """A recent, non-empty snapshot.enc → ok."""
    home = _home(tmp_path, _BACKUP_CFG)
    snap = tmp_path / "snapshot.enc"
    snap.write_bytes(b"data")
    # mtime is current by default (just created) — well within any staleness window
    r = probes.probe_backup(home)
    assert r["state"] == "ok"
    assert r["last_verified"] is not None


def test_backup_needs_action_zero_byte_snapshot(tmp_path):
    """A 0-byte snapshot.enc → needs_action regardless of mtime."""
    home = _home(tmp_path, _BACKUP_CFG)
    snap = tmp_path / "snapshot.enc"
    snap.write_bytes(b"")
    r = probes.probe_backup(home)
    assert r["state"] == "needs_action"
    assert "empty" in r["detail"].lower()


def test_backup_needs_action_stale_snapshot(tmp_path):
    """A snapshot older than the staleness window → needs_action."""
    import os
    import time
    home = _home(tmp_path, _BACKUP_CFG)
    snap = tmp_path / "snapshot.enc"
    snap.write_bytes(b"data")
    # Backdate mtime by 10 days (well beyond default 7-day window)
    old_time = time.time() - (10 * 86400)
    os.utime(str(snap), (old_time, old_time))
    r = probes.probe_backup(home)
    assert r["state"] == "needs_action"
    assert "stale" in r["detail"].lower()
