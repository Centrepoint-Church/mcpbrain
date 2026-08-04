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


def test_backup_needs_action_when_uploads_keep_failing(tmp_path):
    """A fresh snapshot.enc does NOT mean the backup reached Drive.

    The artifact is encrypted locally BEFORE the upload, so every failed
    attempt still refreshes snapshot.enc's mtime -- the probe stayed green
    through the 2026-08-03 storm (57 failed uploads in one day) because each
    failure refreshed the very file being probed. The probe must key off the
    last recorded SUCCESS.
    """
    import json
    import time
    home = _home(tmp_path, _BACKUP_CFG)
    (tmp_path / "snapshot.enc").write_bytes(b"data")  # fresh, as a failed run leaves it
    (tmp_path / "backup_state.json").write_text(json.dumps({
        "last_success": time.time() - (30 * 86400),
        "consecutive_failures": 412,
        "last_error": "Broken pipe",
    }))

    r = probes.probe_backup(home)

    assert r["state"] == "needs_action"
    assert "412" in r["detail"] or "fail" in r["detail"].lower()


def test_backup_ok_when_last_upload_succeeded_recently(tmp_path):
    """A recorded recent success is the positive signal."""
    import json
    import time
    home = _home(tmp_path, _BACKUP_CFG)
    (tmp_path / "snapshot.enc").write_bytes(b"data")
    (tmp_path / "backup_state.json").write_text(json.dumps({
        "last_success": time.time() - 3600,
        "consecutive_failures": 0,
    }))

    assert probes.probe_backup(home)["state"] == "ok"


def test_backup_probe_uses_the_configured_interval(tmp_path):
    """The staleness window must derive from backup.interval_s.

    probe_backup read `interval_seconds`, a key NOTHING in the repo has ever
    written (the writer at backup_setup.py and the daemon's loader both use
    `interval_s`). The branch has been dead since it landed on 2026-06-10, so
    the window was always the 7-day default -- on this box, 7x more lenient
    than the configured 86400. A backup could be six days stale and read "On".
    """
    import json
    import time
    home = _home(tmp_path, {"backup": {"dest": "x", "interval_s": 3600}})
    (tmp_path / "snapshot.enc").write_bytes(b"data")
    (tmp_path / "backup_state.json").write_text(json.dumps({
        "last_success": time.time() - (3 * 3600),   # 3h, past 2x the 1h interval
        "consecutive_failures": 2,
    }))

    assert probes.probe_backup(home)["state"] == "needs_action"


def test_backup_probe_tolerates_a_single_missed_interval(tmp_path):
    """One transient miss must not flag — two consecutive should.

    Deliberately 2x the interval, not 1x: a single failed run is routine (on
    2026-08-04 alone there were transient WAL-busy and broken-pipe failures
    that self-corrected on the next attempt), and flagging each one trains
    people to ignore the indicator.
    """
    import json
    import time
    home = _home(tmp_path, {"backup": {"dest": "x", "interval_s": 3600}})
    (tmp_path / "snapshot.enc").write_bytes(b"data")
    (tmp_path / "backup_state.json").write_text(json.dumps({
        "last_success": time.time() - 5400,   # 1.5h — one interval missed
        "consecutive_failures": 1,
    }))

    assert probes.probe_backup(home)["state"] == "ok"


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
