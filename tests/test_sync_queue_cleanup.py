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


def test_init_now_also_clears_shared_drive_resume_state(tmp_path):
    """sync_shared_drive is deleted (all shared-drive sync now goes through
    discover_shared_drive/handle_shared_drive_item, which use sync_queue and
    never write per-round resume state), so its per-drive resume keys, keyed
    f"drive:{drive_id}:resume_ids" etc., are dead too -- the counterpart to
    the now-removed test_init_spares_shared_drive_resume_state, which
    asserted the opposite while sync_shared_drive was still live."""
    s = Store(tmp_path / "m.sqlite3", dim=4)
    s.init()
    s.set_cursor("drive:D1", "999")
    s.set_cursor("drive:D1:resume_ids", '["a"]')
    s.set_cursor("drive:D1:resume_removed_ids", '["b"]')
    s.set_cursor("drive:D1:page_token", "1000")
    # The shared-drive progressive-backfill floor cursors (_shared_drive_backfill_step)
    # are real, live, unrelated state -- separated from the drive id by an
    # underscore, never a second colon, so they cannot match the widened LIKE
    # patterns above ('drive:%:resume_ids' etc.) by construction. Asserted
    # explicitly here (final-review Finding 8) because the whole point of this
    # test file is guarding against an over-broad match wiping live state --
    # a test that only checks the ONE key format the fix targets doesn't prove
    # the fix stayed narrow.
    s.set_cursor("drive:D1_backfill_until", "2026-01-01T00:00:00")
    s.set_cursor("drive:D1_backfill_empty", "2")
    s.init()
    assert s.get_cursor("drive:D1") == "999", "the real per-drive cursor must survive"
    assert s.get_cursor("drive:D1_backfill_until") == "2026-01-01T00:00:00", \
        "the backfill floor cursor must survive"
    assert s.get_cursor("drive:D1_backfill_empty") == "2", \
        "the backfill empty-window counter must survive"
    for dead in ("drive:D1:resume_ids", "drive:D1:resume_removed_ids",
                "drive:D1:page_token"):
        assert s.get_cursor(dead) is None, f"{dead} not cleaned up"
