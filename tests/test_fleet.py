"""Unit tests for mcpbrain.fleet — Drive mocked at the drive_service boundary."""
from datetime import datetime, timedelta, timezone

from mcpbrain import fleet


def _beacon(email, *, ver="0.6.0", reported_at=None, probes=None):
    if reported_at is None:
        reported_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return {
        "user_email": email,
        "mcpbrain_version": ver,
        "reported_at": reported_at,
        "probes": probes or {
            "google": {"state": "ok", "detail": "Connected"},
            "claude": {"state": "ok", "detail": ""},
            "clickup": {"state": "needs_action", "detail": "API key missing"},
            "backup": {"state": "ok", "detail": ""},
            "records": {"state": "not_started", "detail": ""},
            "enrichment": {"state": "ok", "detail": ""},
        },
    }


def test_generate_report_renders_one_row_per_user_with_colour_classes():
    html = fleet.generate_report([_beacon("john@centrepoint.church")])
    assert "john@centrepoint.church" in html
    assert "0.6.0" in html
    # colour-coded probe cells: green=ok, amber=needs_action, grey=not_started
    assert "probe-ok" in html
    assert "probe-needs_action" in html
    assert "probe-not_started" in html
    assert "Last generated" in html


def test_generate_report_flags_stale_rows_over_48h():
    old = (datetime.now(timezone.utc) - timedelta(hours=49)).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = fleet.generate_report([_beacon("mike@centrepoint.church", reported_at=old)])
    assert "stale" in html  # ⚠️ stale badge present on the row


def test_generate_report_fresh_row_not_stale():
    fresh = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    html = fleet.generate_report([_beacon("sarah@centrepoint.church", reported_at=fresh)])
    # the fresh row must not carry the stale badge
    assert 'class="stale"' not in html
