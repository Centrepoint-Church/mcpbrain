"""Connection probes: each returns a verified tri-state the UI renders.

A probe answers "is this connection working?" from config + local filesystem
only (no network), so it is cheap enough for the wizard's status poll. State is
one of: "not_started" (never configured), "ok" (configured + verified), or
"needs_action" (configured but broken / incomplete — the UI shows a fix button).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcpbrain import auth, config

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


def probe_claude(home) -> dict:
    """Verified when the MCP server has written its heartbeat recently enough."""
    p = Path(home) / "mcp_heartbeat.json"
    if not p.exists():
        return _state("not_started", "Not connected yet — quit & reopen Claude Desktop")
    try:
        last = json.loads(p.read_text()).get("last_seen")
        if last is None:
            raise ValueError("missing last_seen")
        last_dt = datetime.fromisoformat(last)
        # Ensure timezone-aware for comparison
        if last_dt.tzinfo is None:
            last_dt = last_dt.replace(tzinfo=timezone.utc)
        age = datetime.now(timezone.utc) - last_dt
        if age > timedelta(days=_CLAUDE_STALE_DAYS):
            return _state("needs_action", "Not seen recently — open Claude Desktop",
                          last_verified=last)
    except (OSError, ValueError):
        return _state("needs_action", "Not seen recently — open Claude Desktop")
    return _state("ok", "Connected to Claude Desktop", last_verified=last)


def probe_clickup(home) -> dict:
    key = config.clickup_api_key(home).strip()
    if not key:
        return _state("not_started", "Not connected")
    if not config.clickup_list_id(home).strip():
        return _state("needs_action", "API key set but no list selected")
    if not config.user_timezone(home):
        return _state("needs_action", "Set your timezone (required for deadlines)")
    return _state("ok", "Connected")


def probe_backup(home) -> dict:
    cfg = config.read_config(home)
    if not cfg.get("backup"):
        return _state("not_started", "Backup off")
    snap = Path(home) / "snapshot.enc"
    return _state("ok", "On", last_verified=_mtime(snap) if snap.exists() else None)


def probe_records(home) -> dict:
    repo = Path(config.records_dir(home))
    if (repo / ".git").is_dir():
        return _state("ok", str(repo))
    return _state("not_started", "Records repo not created yet")


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
    }
    out = {}
    for name, live in cheap.items():
        c = cached.get(name)
        # Verified cache wins when present; cheap live state covers the gap
        # for connections not yet in the cache.
        if c:
            out[name] = c
        else:
            out[name] = live
    return out
