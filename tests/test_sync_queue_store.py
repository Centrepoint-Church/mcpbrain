"""sync_queue: the durable seam between discovery and work.

The invariant everything rests on: a page's rows and that page's cursor
advance commit TOGETHER, so the cursor can never be ahead of what is recorded.
"""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "q.sqlite3", dim=4)
    s.init()
    return s


def _item(ref, *, version="1", event="upsert", modified_at="2026-09-04T10:00:00"):
    return {"ref_id": ref, "version": version, "event": event,
            "modified_at": modified_at}


def test_enqueue_and_advance_writes_rows_and_cursor(tmp_path):
    s = _store(tmp_path)
    n = s.enqueue_and_advance([_item("f1"), _item("f2")], source="drive", cursor="200")
    assert n == 2
    assert s.sync_queue_pending("drive") == 2
    assert s.get_cursor("drive") == "200"


def test_reenqueue_same_ref_collapses_to_one_row(tmp_path):
    """PRIMARY KEY (source, ref_id): a later event supersedes the earlier one."""
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1", event="upsert")], source="drive", cursor="1")
    s.enqueue_and_advance([_item("f1", event="remove", version="2")],
                          source="drive", cursor="2")
    assert s.sync_queue_pending("drive") == 1
    row = s.due_sync_items(limit=10, now="2026-09-04T12:00:00")[0]
    assert row["event"] == "remove"


def test_pending_is_scoped_by_source(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1")], source="drive", cursor="1")
    s.enqueue_and_advance([_item("m1")], source="gmail", cursor="9")
    assert s.sync_queue_pending("drive") == 1
    assert s.sync_queue_pending("gmail") == 1
    assert s.sync_queue_pending() == 2


def test_cursor_and_rows_are_one_transaction(tmp_path):
    """A failure mid-write must leave BOTH unchanged, never a cursor ahead of rows."""
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1")], source="drive", cursor="100")
    bad = [_item("f2"), {"ref_id": None, "version": "1", "event": "upsert",
                         "modified_at": "2026-09-04T10:00:00"}]
    try:
        s.enqueue_and_advance(bad, source="drive", cursor="999")
    except Exception:
        pass
    assert s.get_cursor("drive") == "100", "cursor advanced despite a failed write"
    assert s.sync_queue_pending("drive") == 1


def test_due_items_are_newest_first(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([
        _item("old", modified_at="2026-07-01T00:00:00"),
        _item("new", modified_at="2026-09-04T00:00:00"),
        _item("mid", modified_at="2026-08-01T00:00:00"),
    ], source="drive", cursor="1")
    got = [r["ref_id"] for r in s.due_sync_items(limit=10, now="2026-09-05T00:00:00")]
    assert got == ["new", "mid", "old"]


def test_due_items_respect_limit(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item(f"f{i}", modified_at=f"2026-09-0{i}T00:00:00")
                           for i in range(1, 6)], source="drive", cursor="1")
    assert len(s.due_sync_items(limit=2, now="2026-09-09T00:00:00")) == 2


def test_backed_off_item_is_not_returned_and_does_not_block(tmp_path):
    """No head-of-line blocking: a failing item must not stall the queue."""
    s = _store(tmp_path)
    s.enqueue_and_advance([
        _item("poison", modified_at="2026-09-04T00:00:00"),
        _item("healthy", modified_at="2026-09-03T00:00:00"),
    ], source="drive", cursor="1")
    s.fail_sync_item("drive", "poison", "export timeout", now="2026-09-04T10:00:00")
    got = [r["ref_id"] for r in s.due_sync_items(limit=10, now="2026-09-04T10:01:00")]
    assert got == ["healthy"], "a backed-off item blocked the newest-first queue"


def test_due_query_is_index_backed_on_an_existing_store(tmp_path):
    """0.7.105 lesson: a fresh store's DDL and query text always agree, so a
    fresh-store test cannot catch drift. Re-init an ALREADY-init'd store and
    assert the plan still uses the index."""
    s = _store(tmp_path)
    s.init()  # second init, as a real upgrade does
    with s._connect() as db:
        plan = " ".join(str(list(r)) for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM sync_queue "
            "WHERE next_attempt_at IS NULL OR next_attempt_at <= ? "
            "ORDER BY modified_at DESC LIMIT ?", ("2026-09-04", 50)).fetchall())
    assert "SCAN sync_queue" not in plan, f"full scan: {plan}"
    assert "idx_sync_queue_due" in plan, f"index unused: {plan}"


def test_complete_removes_the_row(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1")], source="drive", cursor="1")
    s.complete_sync_item("drive", "f1")
    assert s.sync_queue_pending("drive") == 0


def test_failure_backs_off_and_never_drops(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1")], source="drive", cursor="1")
    for expected in (1, 2, 3):
        assert s.fail_sync_item("drive", "f1", "boom",
                                now="2026-09-04T10:00:00") == expected
    assert s.sync_queue_pending("drive") == 1, "an item was dropped"
    row = s.due_sync_items(limit=10, now="2099-01-01T00:00:00")[0]
    assert row["attempts"] == 3
    assert row["last_error"] == "boom"


def test_new_version_resets_attempts(tmp_path):
    """A genuinely new edit is new work, not a continuation of a failing one."""
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1", version="1")], source="drive", cursor="1")
    s.fail_sync_item("drive", "f1", "boom", now="2026-09-04T10:00:00")
    s.enqueue_and_advance([_item("f1", version="2")], source="drive", cursor="2")
    row = s.due_sync_items(limit=10, now="2026-09-04T10:00:01")[0]
    assert row["attempts"] == 0
    assert row["next_attempt_at"] is None


def test_stats_surface_pending_age_and_failures(tmp_path):
    s = _store(tmp_path)
    s.enqueue_and_advance([_item("f1"), _item("f2")], source="drive", cursor="1")
    for _ in range(3):
        s.fail_sync_item("drive", "f1", "export timeout", now="2026-09-04T10:00:00")
    st = s.sync_queue_stats()
    assert st["pending"] == 2
    assert st["oldest_discovered_at"] is not None
    assert [f["ref_id"] for f in st["failing"]] == ["f1"]
    assert st["failing"][0]["attempts"] == 3
