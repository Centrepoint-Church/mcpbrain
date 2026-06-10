"""Connection probes: each returns a verified tri-state the UI renders.

A probe answers "is this connection working?" from config + local filesystem
only (no network), so it is cheap enough for the wizard's status poll. State is
one of: "not_started" (never configured), "ok" (configured + verified), or
"needs_action" (configured but broken / incomplete — the UI shows a fix button).
"""
from __future__ import annotations

import json
import time as _time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcpbrain import auth, config, cowork_tasks, hooks

_CLAUDE_STALE_DAYS = 14


def _state(state: str, detail: str = "", last_verified=None) -> dict:
    return {"state": state, "detail": detail, "last_verified": last_verified}


def probe_google(home) -> dict:
    """Token present + locally-valid (no network refresh)."""
    token_file = auth.token_path()
    if not token_file.exists():
        return _state("not_started", "Not signed in")
    try:
        from google.oauth2.credentials import Credentials
        creds = Credentials.from_authorized_user_file(str(token_file), auth.SCOPES)
        valid = bool(creds and (creds.valid or creds.refresh_token))
    except Exception:  # noqa: BLE001 — unreadable token => needs re-auth
        return _state("needs_action", "Sign-in expired — reconnect")
    if not valid:
        return _state("needs_action", "Access expired — reconnect")
    return _state("ok", "Connected", last_verified=_mtime(token_file))


def _claude_registered() -> bool:
    """True when claude_desktop_config.json lists an mcpbrain server entry."""
    try:
        from mcpbrain.wizard.register import claude_desktop_config_path
        p = claude_desktop_config_path()
        data = json.loads(Path(p).read_text())
        servers = data.get("mcpServers") or {}
        return any("mcpbrain" in name for name in servers)
    except (OSError, ValueError, KeyError):
        return False


def probe_claude(home) -> dict:
    """Three states: not registered -> registered/awaiting restart -> connected."""
    registered = _claude_registered()
    p = Path(home) / "mcp_heartbeat.json"
    if not p.exists():
        if not registered:
            return _state("not_started", "Not registered yet — finish setup")
        return _state("needs_action", "Registered — quit & reopen Claude Desktop")
    try:
        last = json.loads(p.read_text()).get("last_seen")
        if last is None:
            raise ValueError("missing last_seen")
        last_dt = datetime.fromisoformat(last)
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        if datetime.now(timezone.utc) - last_dt > timedelta(days=_CLAUDE_STALE_DAYS):
            return _state("needs_action", "Not seen recently — open Claude Desktop", last_verified=last)
    except (OSError, ValueError):
        if not registered:
            return _state("not_started", "Not registered yet — finish setup")
        return _state("needs_action", "Registered — quit & reopen Claude Desktop")
    return _state("ok", "Connected", last_verified=last)


def probe_clickup(home) -> dict:
    key = config.clickup_api_key(home).strip()
    if not key:
        return _state("not_started", "Not connected")
    if not config.clickup_list_id(home).strip():
        return _state("needs_action", "API key set but no list selected")
    if not config.user_timezone(home):
        return _state("needs_action", "Set your timezone (required for deadlines)")
    return _state("ok", "Connected")


def _now_iso() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _verify_clickup(home) -> dict:
    """Real ClickUp check: key+list+tz present AND a test API call resolves the list."""
    base = probe_clickup(home)
    if base["state"] != "ok":
        return base
    try:
        from mcpbrain import clickup
        clickup.list_tasks_full(home, include_closed=False)  # one API call
        return _state("ok", "Verified", last_verified=_now_iso())
    except Exception as exc:  # noqa: BLE001
        return _state("needs_action", f"ClickUp call failed — check the key ({exc.__class__.__name__})")


def _verify_google(home) -> dict:
    """Real Google check: attempt a token refresh (network)."""
    base = probe_google(home)
    if base["state"] == "not_started":
        return base
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        creds = Credentials.from_authorized_user_file(str(auth.token_path()), auth.SCOPES)
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        return _state("ok", "Verified", last_verified=_now_iso())
    except Exception:  # noqa: BLE001
        return _state("needs_action", "Sign-in expired — reconnect")


def verify_connections(home, store=None) -> dict:
    """Run the expensive (network) checks and cache them to connections.json atomically."""
    verified = {"clickup": _verify_clickup(home), "google": _verify_google(home)}
    import os
    import tempfile
    p = Path(home) / "connections.json"
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=".conn.", suffix=".tmp")
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(verified))
    os.replace(tmp, p)
    return verified


_BACKUP_DEFAULT_WINDOW = 7 * 86400  # 7 days in seconds


def probe_backup(home) -> dict:
    cfg = config.read_config(home)
    if not cfg.get("backup"):
        return _state("not_started", "Backup off")
    snap = Path(home) / "snapshot.enc"
    if not snap.exists():
        return _state("needs_action", "No backup snapshot found")
    try:
        st = snap.stat()
    except OSError:
        return _state("needs_action", "Cannot read backup snapshot")
    if st.st_size == 0:
        return _state("needs_action", "Backup file is empty")
    # Determine staleness window: use configured interval if available, else 7 days
    backup_cfg = cfg["backup"]
    if isinstance(backup_cfg, dict) and backup_cfg.get("interval_seconds"):
        window = int(backup_cfg["interval_seconds"])
    else:
        window = _BACKUP_DEFAULT_WINDOW
    import time
    age_seconds = time.time() - st.st_mtime
    if age_seconds > window:
        age_days = int(age_seconds // 86400)
        return _state("needs_action", f"Backup is stale (last {age_days} days ago)")
    return _state("ok", "On", last_verified=_mtime(snap))


def probe_records(home) -> dict:
    repo = Path(config.records_dir(home))
    if (repo / ".git").is_dir():
        detail = "Ready" if (repo / "CLAUDE.md").exists() else "Created (run Prepare working space)"
        return _state("ok", detail)
    return _state("not_started", "Records repo not created yet")


def probe_enrichment(home) -> dict:
    """not_started (no SKILL.md) / ok (running) / needs_action (installed, idle)."""
    if not cowork_tasks.enrichment_skill_present():
        return _state("not_started", "Enrichment skill not installed yet")
    inbox = Path(home) / "enrich_inbox"
    try:
        recent = any(
            (_time.time() - p.stat().st_mtime) < 2 * 86400
            for p in inbox.glob("*.json")
        )
    except OSError:
        recent = False
    if recent:
        return _state("ok", "Running")
    return _state("needs_action", "Set up the schedule in Claude Desktop")


def probe_memory_hooks(home) -> dict:
    return (_state("ok", "On") if hooks.hooks_status().get("installed")
            else _state("not_started", "Off — turn on cross-session memory"))


def _mtime(p: Path):
    try:
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def _read_cache(home) -> dict:
    p = Path(home) / "connections.json"
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def all_connections(home, store=None) -> dict:
    """All connection probes keyed by name. `store` reserved for future probes.

    Overlays a cached verified result (written by a separate cadence) on top of
    cheap live probes. The cache wins when present; the live state takes over
    immediately when a connection is removed (not_started).
    """
    cached = _read_cache(home)
    cheap = {
        "google": probe_google(home),
        "claude": probe_claude(home),
        "clickup": probe_clickup(home),
        "backup": probe_backup(home),
        "records": probe_records(home),
        "enrichment": probe_enrichment(home),
        "memory-hooks": probe_memory_hooks(home),
    }
    out = {}
    for name, live in cheap.items():
        c = cached.get(name)
        # Verified cache wins when the connection is still live (not not_started).
        # If the live probe returns not_started the connection was removed, so the
        # stale cache must not mask that — flip immediately.
        if c and live["state"] != "not_started":
            out[name] = c
        else:
            out[name] = live
    return out
