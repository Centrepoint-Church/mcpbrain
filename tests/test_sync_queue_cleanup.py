"""The obsolete per-round cursor state is removed on upgrade.

resume_ids existed only to remember which files a round had written; rounds no
longer exist. page_token (0.7.123) existed because the cursor could not advance
per page; now it does.
"""
from mcpbrain.store import Store


def test_init_clears_obsolete_round_state(tmp_path):
    s = Store(tmp_path / "m.sqlite3", dim=4)
    s.init()
    s.set_cursor("drive", "336793")
    s.set_cursor("drive:resume_ids", '["a","b"]')
    s.set_cursor("drive:resume_removed_ids", '["c"]')
    s.set_cursor("drive:page_token", "348561")
    s.set_cursor("gmail:resume_ids", '["m1"]')
    s.init()          # upgrade path: init runs again on an existing store
    assert s.get_cursor("drive") == "336793", "the real cursor must survive"
    for dead in ("drive:resume_ids", "drive:resume_removed_ids",
                 "drive:page_token", "gmail:resume_ids"):
        assert s.get_cursor(dead) is None, f"{dead} not cleaned up"


def test_init_spares_shared_drive_resume_state(tmp_path):
    """sync_shared_drive was NOT migrated to discover_*/handle_*_item and
    still leans on its own per-round resume state, keyed
    f"drive:{drive_id}:resume_ids" etc. -- a suffix match like
    `source LIKE '%:resume_ids'` also catches these three-segment keys, which
    would wipe an in-progress shared-drive round on every daemon restart and
    reintroduce, for shared drives specifically, the livelock this whole
    effort exists to fix. Only the bare two-segment drive/gmail/calendar keys
    are actually dead.
    """
    s = Store(tmp_path / "m.sqlite3", dim=4)
    s.init()
    s.set_cursor("drive:SOMESHAREDDRIVEID", "999")
    s.set_cursor("drive:SOMESHAREDDRIVEID:resume_ids", '["a"]')
    s.set_cursor("drive:SOMESHAREDDRIVEID:resume_removed_ids", '["b"]')
    s.set_cursor("drive:SOMESHAREDDRIVEID:page_token", "12345")
    s.init()
    assert s.get_cursor("drive:SOMESHAREDDRIVEID") == "999"
    assert s.get_cursor("drive:SOMESHAREDDRIVEID:resume_ids") == '["a"]'
    assert s.get_cursor("drive:SOMESHAREDDRIVEID:resume_removed_ids") == '["b"]'
    assert s.get_cursor("drive:SOMESHAREDDRIVEID:page_token") == "12345"
