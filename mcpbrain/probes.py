"""Connection probes: each returns a verified tri-state the UI renders.

A probe answers "is this connection working?" from config + local filesystem
only (no network), so it is cheap enough for the wizard's status poll. State is
one of: "not_started" (never configured), "ok" (configured + verified), or
"needs_action" (configured but broken / incomplete — the UI shows a fix button).
"""
from __future__ import annotations

import json
from pathlib import Path

from mcpbrain import auth, config


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
    """Verified when the MCP server has written its heartbeat at least once."""
    p = Path(home) / "mcp_heartbeat.json"
    if not p.exists():
        return _state("not_started", "Not connected yet — quit & reopen Claude Desktop")
    try:
        last = json.loads(p.read_text()).get("last_seen")
    except (OSError, ValueError):
        last = None
    return _state("ok", "Connected to Claude Desktop", last_verified=last)


def probe_clickup(home) -> dict:
    key = config.clickup_api_key(home).strip()
    if not key:
        return _state("not_started", "Not connected")
    if not config.clickup_list_id(home).strip():
        return _state("needs_action", "API key set but no list selected")
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
        from datetime import datetime, timezone
        return datetime.fromtimestamp(p.stat().st_mtime, tz=timezone.utc).isoformat()
    except OSError:
        return None


def all_connections(home, store=None) -> dict:
    """All connection probes keyed by name. `store` reserved for future probes."""
    return {
        "google": probe_google(home),
        "claude": probe_claude(home),
        "clickup": probe_clickup(home),
        "backup": probe_backup(home),
        "records": probe_records(home),
    }
