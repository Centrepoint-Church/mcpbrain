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
from pathlib import Path

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
        # Daemon liveness from the daemon's own heartbeat — independent of the
        # beacon job and of cached probe state. Missing/old => the daemon is down
        # even when the hourly beacon job (this row's "Last seen") is fresh.
        hb_dt = _parse_reported_at(b.get("daemon_heartbeat") or "")
        hb_stale = hb_dt is None or (
            datetime.now(timezone.utc) - hb_dt).total_seconds() > _STALE_HOURS * 3600
        hb_label = "down" if hb_dt is None else _age_label(hb_dt)
        if hb_stale:
            daemon_cell = f'<td class="stale">⚠️ {_html.escape(hb_label)}</td>'
        else:
            daemon_cell = f"<td>{_html.escape(hb_label)}</td>"
        probes = b.get("probes") or {}
        cells = []
        for name in _PROBE_ORDER:
            p = probes.get(name) or {}
            state = p.get("state", "unknown")
            cls = f"probe-{state}" if state in _GLYPH else "probe-unknown"
            glyph = _GLYPH.get(state, "")
            cells.append(f'<td class="{cls}">{glyph}</td>')
        rows.append(
            f"<tr><td>{email}</td><td>{ver}</td>{seen_cell}{daemon_cell}{''.join(cells)}</tr>"
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
<table><thead><tr><th>User</th><th>Ver</th><th>Last seen</th><th>Daemon</th>{headers}</tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody></table>
</body></html>
"""


def _list_all(drive_service, *, q: str, fields: str) -> list[dict]:
    """List every file matching ``q``, following pagination (Shared-Drive aware).

    Drive returns at most ``pageSize`` (max 1000) results per call; without
    looping on ``nextPageToken`` everything past the first page is silently
    dropped. Fleet folders accumulate one beacon per user and never auto-clean,
    so the report must page through all of them.
    """
    out: list[dict] = []
    page_token = None
    while True:
        resp = drive_service.files().list(
            q=q,
            fields=f"nextPageToken, {fields}",
            pageSize=1000,
            supportsAllDrives=True,
            includeItemsFromAllDrives=True,
            pageToken=page_token,
        ).execute()
        out.extend(resp.get("files", []))
        page_token = resp.get("nextPageToken")
        if not page_token:
            break
    return out


def _find_file_id(drive_service, folder_id: str, name: str):
    """Return the id of ``name`` in ``folder_id`` (Shared-Drive aware), or None.

    If duplicates exist (a create/create race can produce two files with the
    same name), returns the most recently modified so update targets the live one.
    """
    files = _list_all(
        drive_service,
        q=f"name='{name}' and '{folder_id}' in parents and trashed=false",
        fields="files(id,name,modifiedTime)",
    )
    if not files:
        return None
    files.sort(key=lambda f: f.get("modifiedTime", ""), reverse=True)
    return files[0]["id"]


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


def _read_daemon_heartbeat(home) -> str | None:
    """Return the daemon's last-cycle ISO timestamp, or None if never written."""
    try:
        data = json.loads((Path(home) / "daemon_heartbeat.json").read_text())
        return data.get("last_cycle")
    except (OSError, ValueError):
        return None


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
            # The daemon's own last-cycle stamp (written by the daemon process).
            # reported_at = when this beacon job ran; daemon_heartbeat = when the
            # daemon last completed a cycle. A live beacon + stale daemon = the
            # daemon is down even though the hourly beacon job still fires.
            "daemon_heartbeat": _read_daemon_heartbeat(home),
            "probes": probes.all_connections(home),
        }
        _upload_text(drive_service, folder_id, f"{email}.json",
                     json.dumps(payload, indent=2), "application/json")
        log.info("fleet beacon written for %s", email)
    except Exception as exc:  # noqa: BLE001 — beacon failure must never crash the daemon
        log.warning("fleet beacon write failed (swallowed): %s", exc)


# Org-config may ONLY override the keys in this ALLOWLIST (default-DENY). A file
# on the shared Drive must never be able to repoint records_dir, clickup_list_id,
# enrich_mode, the org taxonomy, identity, or secrets — so we allow-list rather
# than block-list. `cadences` is the one surface an admin legitimately pushes
# org-wide, and the daemon already range-validates every cadence value
# (_cadences_from_config), so a bad value can only disable a cadence — it cannot
# exfiltrate data or misdirect sync/tasks. Extend this set deliberately, never
# by default.
_ALLOWLIST = frozenset({"cadences"})

# The managed config block org-config is staged into. It is REPLACED wholesale
# on every daemon startup (config.write_config is a shallow merge), so removing a
# key from org-config.json reverts it on the next start, and the user's own
# top-level config keys are never clobbered. Consumers overlay this at read time.
_OVERLAY_KEY = "org_config"


def read_org_config(folder_id: str, drive_service) -> dict:
    """Download and parse ``org-config.json`` from the fleet folder.

    Returns ``{}`` if the file is absent or the download/parse fails. No merge,
    no allowlist applied here — that is ``merge_org_config``'s job.
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
    """Read org-config and stage the allowlisted subset into a managed overlay.

    The allowed keys are written into ``config["org_config"]`` (the overlay
    block), which is REPLACED wholesale on every startup — so removing a key
    from ``org-config.json`` reverts it on the next daemon start, and the user's
    own top-level config keys are never touched. Non-allowlisted keys are
    dropped. Consumers (e.g. the daemon's ``_cadences_from_config``) overlay
    ``config["org_config"]`` at read time. Returns the staged overlay dict.
    """
    from mcpbrain import config
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
    if not folder_id:
        return {}
    org = read_org_config(folder_id, drive_service)
    allowed = {k: v for k, v in org.items() if _is_allowed(k)}
    dropped = sorted(k for k in org if not _is_allowed(k))
    if dropped:
        log.info("org-config: ignoring non-allowlisted keys: %s", dropped)
    # Always write (even when empty) so a cleared/removed org-config.json reverts
    # the overlay on the next startup rather than leaving stale overrides.
    config.write_config(home, {_OVERLAY_KEY: allowed})
    if allowed:
        log.info("org-config: staged overlay keys: %s", sorted(allowed))
    return allowed


def _is_allowed(key: str) -> bool:
    return key in _ALLOWLIST


def _list_beacon_files(drive_service, folder_id: str) -> list[dict]:
    """All beacon ``*.json`` files in the fleet folder (excluding org-config.json).

    Paginates, and de-duplicates by filename keeping the most recently modified —
    a create/create race (hourly cadence overlapping a manual ``--beacon``) can
    leave two ``<email>.json`` files, which would otherwise show the user twice.
    """
    files = _list_all(
        drive_service,
        q=f"'{folder_id}' in parents and trashed=false",
        fields="files(id,name,modifiedTime)",
    )
    newest: dict[str, dict] = {}
    for f in files:
        name = f.get("name", "")
        if name == "org-config.json" or not name.endswith(".json"):
            continue
        prev = newest.get(name)
        if prev is None or f.get("modifiedTime", "") > prev.get("modifiedTime", ""):
            newest[name] = f
    return list(newest.values())


def write_report(home, drive_service) -> None:
    """Aggregate all beacons into ``status.html`` and upload it to the fleet folder.

    Skips malformed beacon JSON (logs a warning). Prints "No beacons found" and
    writes nothing when the folder has no parseable beacons.
    """
    from mcpbrain import config
    folder_id = (config.read_config(home).get("fleet") or {}).get("folder_id")
    if not folder_id:
        print("fleet.folder_id not set — run mcpbrain setup to configure.")
        return
    files = _list_beacon_files(drive_service, folder_id)
    beacons = []
    for f in files:
        try:
            raw = drive_service.files().get_media(
                fileId=f["id"], supportsAllDrives=True).execute()
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8")
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                beacons.append(parsed)
        except Exception as exc:  # noqa: BLE001 — one bad beacon must not abort the report
            log.warning("skipping malformed beacon %s: %s", f.get("name"), exc)
    if not beacons:
        print("No beacons found")
        return
    html = generate_report(beacons)
    _upload_text(drive_service, folder_id, "status.html", html, "text/html")
    log.info("fleet report written: %d beacon(s)", len(beacons))
