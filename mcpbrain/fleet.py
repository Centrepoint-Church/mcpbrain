"""Org fleet visibility: per-user health beacons + aggregated HTML report.

All Drive I/O goes through the user's existing OAuth ``drive_service`` resource.
Pure Python — no LLM, no Anthropic API, no background Claude. Beacon-write
errors are logged and swallowed so a failed beacon never affects the daemon.
"""
from __future__ import annotations

import html as _html
import json
import logging
from datetime import datetime, timezone

log = logging.getLogger(__name__)

_STALE_HOURS = 48
_PROBE_ORDER = ("google", "claude", "clickup", "backup", "records", "enrichment")
_PROBE_LABELS = {
    "google": "Google", "claude": "Claude", "clickup": "ClickUp",
    "backup": "Backup", "records": "Records", "enrichment": "Enrichment",
}
_GLYPH = {"ok": "✅", "needs_action": "⚠️", "not_started": "❌"}


def _parse_reported_at(value: str):
    """Parse an ISO timestamp (with or without trailing Z) to aware UTC, or None."""
    if not value:
        return None
    try:
        v = value.replace("Z", "+00:00")
        dt = datetime.fromisoformat(v)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _age_label(dt) -> str:
    if dt is None:
        return "unknown"
    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600.0
    if hours < 1:
        return "<1h ago"
    if hours < 48:
        return f"{int(hours)}h ago"
    return f"{int(hours // 24)}d ago"


def generate_report(beacons: list[dict]) -> str:
    """Render a fleet-status HTML table — pure function, no Drive calls.

    One row per beacon: version, last-seen (with a ``stale`` badge when
    ``reported_at`` is >48h old), and one colour-coded cell per probe
    (probe-ok / probe-needs_action / probe-not_started; unknown -> probe-unknown).
    """
    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    rows = []
    for b in sorted(beacons, key=lambda x: x.get("user_email", "")):
        email = _html.escape(str(b.get("user_email", "")))
        ver = _html.escape(str(b.get("mcpbrain_version", "")))
        dt = _parse_reported_at(b.get("reported_at", ""))
        stale = dt is None or (datetime.now(timezone.utc) - dt).total_seconds() > _STALE_HOURS * 3600
        seen = _age_label(dt)
        if stale:
            seen_cell = f'<td class="stale">⚠️ {_html.escape(seen)}</td>'
        else:
            seen_cell = f"<td>{_html.escape(seen)}</td>"
        probes = b.get("probes") or {}
        cells = []
        for name in _PROBE_ORDER:
            p = probes.get(name) or {}
            state = p.get("state", "unknown")
            cls = f"probe-{state}" if state in _GLYPH else "probe-unknown"
            glyph = _GLYPH.get(state, "")
            cells.append(f'<td class="{cls}">{glyph}</td>')
        rows.append(
            f"<tr><td>{email}</td><td>{ver}</td>{seen_cell}{''.join(cells)}</tr>"
        )
    headers = "".join(f"<th>{_PROBE_LABELS[n]}</th>" for n in _PROBE_ORDER)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>mcpbrain Fleet Status</title>
<style>
body{{font-family:-apple-system,system-ui,sans-serif;margin:2rem;}}
table{{border-collapse:collapse;width:100%;}}
th,td{{border:1px solid #ddd;padding:6px 10px;text-align:left;}}
.probe-ok{{background:#d7f5dd;}}
.probe-needs_action{{background:#ffe9b3;}}
.probe-not_started{{background:#eee;color:#888;}}
.probe-unknown{{background:#eee;color:#888;}}
.stale{{color:#b00;font-weight:600;}}
</style></head><body>
<h1>mcpbrain Fleet Status</h1>
<p>Last generated {generated}</p>
<table><thead><tr><th>User</th><th>Ver</th><th>Last seen</th>{headers}</tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table>
</body></html>
"""


def _find_file_id(drive_service, folder_id: str, name: str):
    """Return the id of ``name`` in ``folder_id`` (Shared-Drive aware), or None."""
    resp = drive_service.files().list(
        q=f"name='{name}' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name)",
        supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    files = resp.get("files", [])
    return files[0]["id"] if files else None


def _upload_text(drive_service, folder_id: str, name: str, text: str, mimetype: str) -> None:
    """Create or update ``name`` in ``folder_id`` with ``text`` (Shared-Drive aware)."""
    from googleapiclient.http import MediaInMemoryUpload
    media = MediaInMemoryUpload(text.encode("utf-8"), mimetype=mimetype)
    existing = _find_file_id(drive_service, folder_id, name)
    if existing:
        drive_service.files().update(
            fileId=existing, media_body=media, supportsAllDrives=True,
        ).execute()
    else:
        meta = {"name": name, "parents": [folder_id]}
        drive_service.files().create(
            body=meta, media_body=media, fields="id", supportsAllDrives=True,
        ).execute()


def write_beacon(home, drive_service) -> None:
    """Build this install's health beacon and upload it as ``<owner_email>.json``.

    Payload = ``probes.all_connections(home)`` plus ``user_email``,
    ``mcpbrain_version``, ``reported_at`` (UTC ISO, trailing Z). All errors are
    logged and swallowed — a failed beacon write never affects the daemon.
    """
    try:
        from mcpbrain import __version__, config, probes
        folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
        if not folder_id:
            return  # fleet not configured -> silently skip
        email = config.owner_email(home)
        if not email:
            return
        payload = {
            "user_email": email,
            "mcpbrain_version": __version__,
            "reported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "probes": probes.all_connections(home),
        }
        _upload_text(drive_service, folder_id, f"{email}.json",
                     json.dumps(payload, indent=2), "application/json")
        log.info("fleet beacon written for %s", email)
    except Exception as exc:  # noqa: BLE001 — beacon failure must never crash the daemon
        log.warning("fleet beacon write failed (swallowed): %s", exc)


# Keys org-config may NEVER override (secrets + identity + machine bindings).
# Any of these in org-config.json is silently dropped regardless of value.
_BLOCKLIST = frozenset({
    "owner_email", "owner_name", "owner_full_name", "owner_role",
    "clickup_api_key", "fleet", "backup",
})


def _is_blocklisted(key: str) -> bool:
    if key in _BLOCKLIST:
        return True
    # Drop any OAuth token field (e.g. google_token, *_token, token, credentials).
    lower = key.lower()
    return "token" in lower or "credential" in lower or lower.endswith("_secret")


def read_org_config(folder_id: str, drive_service) -> dict:
    """Download and parse ``org-config.json`` from the fleet folder.

    Returns ``{}`` if the file is absent or the download/parse fails. No merge,
    no blocklist applied here — that is ``merge_org_config``'s job.
    """
    try:
        file_id = _find_file_id(drive_service, folder_id, "org-config.json")
        if not file_id:
            return {}
        raw = drive_service.files().get_media(fileId=file_id, supportsAllDrives=True).execute()
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:  # noqa: BLE001 — org-config is best-effort
        log.warning("org-config download failed (using local config): %s", exc)
        return {}


def merge_org_config(home, drive_service) -> dict:
    """Read org-config and persist the allowed overrides into local config.

    Shallow merge: each top-level org-config key is applied unless it is
    blocklisted (secrets, identity, fleet/backup bindings, any OAuth token).
    Returns the dict of keys actually applied (empty if none).
    """
    from mcpbrain import config
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
    if not folder_id:
        return {}
    org = read_org_config(folder_id, drive_service)
    allowed = {k: v for k, v in org.items() if not _is_blocklisted(k)}
    dropped = [k for k in org if _is_blocklisted(k)]
    if dropped:
        log.info("org-config: ignoring blocklisted keys: %s", sorted(dropped))
    if allowed:
        config.write_config(home, allowed)
        log.info("org-config: applied keys: %s", sorted(allowed))
    return allowed
