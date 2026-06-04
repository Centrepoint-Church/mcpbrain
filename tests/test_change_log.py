"""change_log: the audit trail behind the dashboard's change digest."""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_record_and_read_changes(tmp_path):
    s = _store(tmp_path)
    s.record_change("capture_ingest", ref_id="note-abc", summary="Saved note 'T'")
    s.record_change("capture_action", ref_id="41", summary="Created action",
                    detail="Do thing", revert_ref="")
    rows = s.recent_changes(limit=10)
    assert len(rows) == 2
    assert rows[0]["change_type"] == "capture_action"  # newest first
    assert rows[1]["ref_id"] == "note-abc"


def test_recent_changes_respects_limit(tmp_path):
    s = _store(tmp_path)
    for i in range(5):
        s.record_change("x", ref_id=str(i), summary="s")
    assert len(s.recent_changes(limit=3)) == 3


def test_open_findings_count(tmp_path):
    s = _store(tmp_path)
    assert s.open_findings_count() == 0
    s.record_finding("org_unrecognised", ref_id="rotary club", summary="x")
    assert s.open_findings_count() == 1


def test_resolve_finding(tmp_path):
    s = _store(tmp_path)
    s.record_finding("org_unrecognised", ref_id="rotary club", summary="x")
    fid = s.open_findings()[0]["id"]
    assert s.resolve_finding(fid) is True
    assert s.open_findings_count() == 0
    assert s.resolve_finding(99999) is False


def test_find_open_action_by_fingerprint(tmp_path):
    s = _store(tmp_path)
    aid = s.add_unified_action(text="Do thing", text_fingerprint="fp-1")
    assert s.find_open_action_by_fingerprint("fp-1") == aid
    assert s.find_open_action_by_fingerprint("fp-none") is None
    s.set_action_status(aid, "done")
    assert s.find_open_action_by_fingerprint("fp-1") is None


def test_prune_change_log_keeps_most_recent(tmp_path):
    s = _store(tmp_path)
    for i in range(10):
        s.record_change("t", ref_id=str(i), summary=f"row {i}")
    pruned = s.prune_change_log(keep=3)
    assert pruned == 7
    remaining = s.recent_changes(20)
    assert len(remaining) == 3
    # the most recent 3 rows are kept
    assert remaining[0]["summary"] == "row 9"
    assert remaining[-1]["summary"] == "row 7"


def test_prune_change_log_noop_when_under_limit(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        s.record_change("t", summary=f"row {i}")
    pruned = s.prune_change_log(keep=10)
    assert pruned == 0
    assert len(s.recent_changes(20)) == 3


def test_record_change_carries_source(tmp_path):
    s = _store(tmp_path)
    s.record_change("profile_correction", ref_id="e-1", summary="role updated",
                    source="profile_audit")
    row = s.recent_changes(1)[0]
    assert row["source"] == "profile_audit"


def test_source_defaults_empty_for_existing_callers(tmp_path):
    s = _store(tmp_path)
    s.record_change("capture_ingest", ref_id="n-1", summary="x")
    assert s.recent_changes(1)[0]["source"] == ""
