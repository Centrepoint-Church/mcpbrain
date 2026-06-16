import json
from mcpbrain.store import Store

def _store(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    return s

class TestMeetingPacksTable:
    def test_upsert_and_get(self, tmp_path):
        s = _store(tmp_path)
        s.upsert_meeting_pack("evt1", "Budget Review", "2026-06-06",
                              "## Agenda\n- Quarterly numbers",
                              attendees=["Alice", "Bob"])
        pack = s.get_meeting_pack("evt1")
        assert pack is not None
        assert pack["event_title"] == "Budget Review"
        assert json.loads(pack["attendees"]) == ["Alice", "Bob"]
        assert "Agenda" in pack["pack_text"]

    def test_upsert_overwrites(self, tmp_path):
        s = _store(tmp_path)
        s.upsert_meeting_pack("evt1", "Old title", "2026-06-06", "old")
        s.upsert_meeting_pack("evt1", "New title", "2026-06-06", "new")
        pack = s.get_meeting_pack("evt1")
        assert pack["event_title"] == "New title"

    def test_get_missing_returns_none(self, tmp_path):
        s = _store(tmp_path)
        assert s.get_meeting_pack("nope") is None

    def test_context_hash_roundtrips_and_updates(self, tmp_path):
        s = _store(tmp_path)
        s.upsert_meeting_pack("evt1", "Budget", "2026-06-06", "v1",
                              attendees=["Alice"], context_hash="h1")
        assert s.get_meeting_pack("evt1")["context_hash"] == "h1"
        # a later run with changed context stores the new hash
        s.upsert_meeting_pack("evt1", "Budget", "2026-06-06", "v2",
                              attendees=["Alice", "Bob"], context_hash="h2")
        assert s.get_meeting_pack("evt1")["context_hash"] == "h2"

    def test_context_hash_defaults_empty_when_omitted(self, tmp_path):
        s = _store(tmp_path)
        s.upsert_meeting_pack("evt1", "Budget", "2026-06-06", "v1")
        assert s.get_meeting_pack("evt1")["context_hash"] == ""

    def test_pack_event_ids_for_date(self, tmp_path):
        s = _store(tmp_path)
        s.upsert_meeting_pack("evt1", "A", "2026-06-06", "x")
        s.upsert_meeting_pack("evt2", "B", "2026-06-06", "y")
        s.upsert_meeting_pack("evt3", "C", "2026-06-07", "z")
        ids = s.pack_event_ids_for_date("2026-06-06")
        assert ids == {"evt1", "evt2"}
        assert "evt3" not in ids

class TestDraftRecordsTable:
    def test_save_and_get(self, tmp_path):
        s = _store(tmp_path)
        draft_id = s.save_draft(
            email_id="msg1", thread_id="thr1", intent="reply",
            audience_tier="staff_internal",
            draft_text="Hi Alice, thanks for reaching out.",
            critique="Good tone, direct.", voice_issues=[],
            samples_used=3, model="claude-sonnet-4-6")
        assert draft_id > 0
        rec = s.get_draft(draft_id)
        assert rec["draft_text"] == "Hi Alice, thanks for reaching out."
        assert rec["audience_tier"] == "staff_internal"
        assert json.loads(rec["voice_issues"]) == []

    def test_get_missing_returns_none(self, tmp_path):
        s = _store(tmp_path)
        assert s.get_draft(9999) is None

    def test_refinement_chain(self, tmp_path):
        s = _store(tmp_path)
        parent_id = s.save_draft(
            email_id="msg1", thread_id="thr1", intent="reply",
            audience_tier="staff_internal", draft_text="v1",
            critique="ok", voice_issues=[], samples_used=0,
            model="claude-sonnet-4-6")
        child_id = s.save_draft(
            email_id="msg1", thread_id="thr1", intent="reply",
            audience_tier="staff_internal", draft_text="v2",
            critique="better", voice_issues=[], samples_used=0,
            model="claude-sonnet-4-6",
            parent_draft_id=parent_id, refinement="warmer")
        child = s.get_draft(child_id)
        assert child["parent_draft_id"] == parent_id
        assert child["refinement"] == "warmer"
