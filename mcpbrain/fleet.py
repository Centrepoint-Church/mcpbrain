"""Org fleet visibility: per-user health beacons + aggregated HTML report.

All Drive I/O goes through the user's existing OAuth ``drive_service`` resource.
Pure Python — no LLM, no Anthropic API, no background Claude. Beacon-write
errors are logged and swallowed so a failed beacon never affects the daemon.
"""
from __future__ import annotations

import html as _html
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
