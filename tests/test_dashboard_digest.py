"""Change digest: assemble() carries recent changes + open findings; the
control API can dismiss a finding."""
import json
import urllib.error
import urllib.request
from unittest import mock

from mcpbrain import dashboard
from mcpbrain.control_api import ControlServer
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_assemble_includes_changes_and_findings(tmp_path):
    s = _store(tmp_path)
    s.record_change("capture_ingest", ref_id="note-1", summary="Saved note")
    s.record_finding("org_unrecognised", ref_id="rotary club", summary="seen")
    with mock.patch("mcpbrain.dashboard.calendar_today", return_value=[]), \
         mock.patch("mcpbrain.dashboard.clickup_today", return_value=[]):
        out = dashboard.assemble(s, str(tmp_path))
    assert out["changes"][0]["summary"] == "Saved note"
    assert out["findings"][0]["ref_id"] == "rotary club"


class FakeDaemon:
    def status(self):
        return {"paused": False, "chunk_count": 0, "google_connected": False,
                "granted_scopes": [], "enrich_enabled": False}


def test_post_dismiss_finding(tmp_path):
    s = _store(tmp_path)
    s.record_finding("org_unrecognised", ref_id="x", summary="s")
    fid = s.open_findings()[0]["id"]
    srv = ControlServer(FakeDaemon(), home=str(tmp_path), store=s)
    srv.start()
    try:
        req = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/api/dashboard/findings/{fid}/dismiss",
            data=b"{}", method="POST")
        req.add_header("Authorization", f"Bearer {srv.token}")
        out = json.loads(urllib.request.urlopen(req).read())
        assert out["dismissed"] is True
        assert s.open_findings_count() == 0
        # Second dismiss must return 404
        req2 = urllib.request.Request(
            f"http://127.0.0.1:{srv.port}/api/dashboard/findings/{fid}/dismiss",
            data=b"{}", method="POST")
        req2.add_header("Authorization", f"Bearer {srv.token}")
        try:
            urllib.request.urlopen(req2)
            assert False, "expected 404"
        except urllib.error.HTTPError as e:
            assert e.code == 404
    finally:
        srv.stop()


def test_changes_digest_degrades_on_store_error(tmp_path):
    """changes_digest must return empty lists rather than crashing on a store error."""
    from unittest import mock
    from mcpbrain.dashboard import changes_digest
    s = _store(tmp_path)
    with mock.patch.object(s, "recent_changes", side_effect=RuntimeError("db gone")):
        result = changes_digest(s)
    assert result == {"changes": [], "findings": []}


# ---------------------------------------------------------------------------
# snooze: Store.snooze_action + dashboard listing filter
# ---------------------------------------------------------------------------

from datetime import datetime, timedelta, timezone

_PERTH = timezone(timedelta(hours=8))


def _perth_date(offset_days: int = 0) -> str:
    return (datetime.now(_PERTH).date() + timedelta(days=offset_days)).isoformat()


def test_snooze_action_sets_column_and_logs_revert_ref(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Snooze me", status="open")
    future = _perth_date(3)

    assert s.snooze_action(aid, future) is True

    row = s.list_unified_actions()[0]
    assert row["snoozed_until"] == future

    change = s.recent_changes(limit=1)[0]
    assert change["change_type"] == "action_snoozed"
    assert change["ref_id"] == str(aid)
    assert change["source"] == "dashboard"
    # Prior value (empty) carried in revert_ref so the snooze can be undone.
    assert change["revert_ref"] == "snoozed_until:"


def test_snooze_action_missing_returns_false(tmp_path):
    s = _store(tmp_path)
    assert s.snooze_action(99999, _perth_date(1)) is False


def test_snooze_action_closed_returns_false(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Closed", status="done")
    assert s.snooze_action(aid, _perth_date(1)) is False
    # No change_log row written for a no-op.
    assert s.recent_changes(limit=5) == []


def test_snooze_action_bad_date_raises_and_no_mutation(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Don't touch", status="open")
    import pytest
    with pytest.raises(ValueError):
        s.snooze_action(aid, "not-a-date")
    row = s.list_unified_actions()[0]
    assert (row["snoozed_until"] or "") == ""
    assert s.recent_changes(limit=5) == []


def test_snoozed_future_action_absent_from_actions_today(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Hidden until later", status="open",
                               deadline=_perth_date(0))
    s.snooze_action(aid, _perth_date(2))
    out = dashboard.actions_today(s)
    texts = [a["text"] for bucket in out.values() for a in bucket]
    assert "Hidden until later" not in texts


def test_snoozed_past_action_reappears_in_actions_today(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Reappeared", status="open",
                               deadline=_perth_date(0))
    # snoozed_until in the past (or today) -> visible again.
    s.snooze_action(aid, _perth_date(-1))
    out = dashboard.actions_today(s)
    texts = [a["text"] for bucket in out.values() for a in bucket]
    assert "Reappeared" in texts
