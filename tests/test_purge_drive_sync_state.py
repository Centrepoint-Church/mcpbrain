"""Store.purge_drive_sync_state: revocation cleanup for the per-drive state the
sync-queue migration introduced.

ingest_cache.purge_drive (deliberately untouched by that migration -- it predates
it and stays scoped to chunks/relations) has no idea sync_queue, sync_cursors, or
shared_drive_pending_publish rows exist for a drive. Once a drive is revoked
(ingest_cache.note_drive_presence purges it), those rows become permanently
failing/orphaned: a queued item for a gone drive KeyErrors forever (the bug fixed
in the final review), and its cursor/pending-publish rows just sit there.

This is the run_sync_cycle-side cleanup the final review's Finding 5 flagged as
real, deliberately-deferred follow-up work.
"""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "p.sqlite3", dim=4)
    s.init()
    return s


def test_purge_drive_sync_state_clears_queue_cursor_and_pending_publish(tmp_path):
    s = _store(tmp_path)
    s.set_cursor("drive:D1", "999")
    s.set_cursor("drive:D1_backfill_until", "2026-01-01T00:00:00")
    s.set_cursor("drive:D1_backfill_empty", "2")
    s.enqueue_and_advance(
        [{"ref_id": "f1", "version": "1", "event": "upsert",
          "modified_at": "2026-09-08T00:00:00"}], source="drive:D1", cursor="999")
    s.record_pending_publish("D1", "f1", "hash1")

    counts = s.purge_drive_sync_state("D1")

    assert s.get_cursor("drive:D1") is None
    assert s.get_cursor("drive:D1_backfill_until") is None
    assert s.get_cursor("drive:D1_backfill_empty") is None
    assert s.sync_queue_pending("drive:D1") == 0
    assert s.pending_publishes("D1") == []
    assert counts == {"queue_rows": 1, "cursors": 3, "pending_publishes": 1}


def test_purge_drive_sync_state_does_not_touch_other_drives(tmp_path):
    s = _store(tmp_path)
    s.set_cursor("drive:D1", "1")
    s.set_cursor("drive:D2", "2")
    s.enqueue_and_advance(
        [{"ref_id": "f1", "version": "1", "event": "upsert",
          "modified_at": "2026-09-08T00:00:00"}], source="drive:D1", cursor="1")
    s.enqueue_and_advance(
        [{"ref_id": "f2", "version": "1", "event": "upsert",
          "modified_at": "2026-09-08T00:00:00"}], source="drive:D2", cursor="2")
    s.record_pending_publish("D2", "f2", "hash2")

    s.purge_drive_sync_state("D1")

    assert s.get_cursor("drive:D2") == "2"
    assert s.sync_queue_pending("drive:D2") == 1
    assert s.pending_publishes("D2") == [("f2", "hash2")]


def test_purge_drive_sync_state_is_a_safe_no_op_for_an_unknown_drive(tmp_path):
    s = _store(tmp_path)
    counts = s.purge_drive_sync_state("NEVER-SEEN")
    assert counts == {"queue_rows": 0, "cursors": 0, "pending_publishes": 0}


def test_purge_drive_sync_state_leaves_my_drive_and_other_sources_alone(tmp_path):
    """A shared drive id must never collide with 'drive' (My Drive), 'gmail',
    or 'calendar' sources -- exact match on drive:<id>, never a substring."""
    s = _store(tmp_path)
    s.set_cursor("drive", "100")
    s.set_cursor("gmail", "200")
    s.enqueue_and_advance(
        [{"ref_id": "mydrive-f", "version": "1", "event": "upsert",
          "modified_at": "2026-09-08T00:00:00"}], source="drive", cursor="100")
    s.enqueue_and_advance(
        [{"ref_id": "shared-f", "version": "1", "event": "upsert",
          "modified_at": "2026-09-08T00:00:00"}], source="drive:D1", cursor="1")

    s.purge_drive_sync_state("D1")

    assert s.get_cursor("drive") == "100"
    assert s.get_cursor("gmail") == "200"
    assert s.sync_queue_pending("drive") == 1
