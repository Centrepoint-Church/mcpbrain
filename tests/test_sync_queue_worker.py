"""The shared work loop: claim newest-first, handle, complete or back off.

A budget cutoff here must be FREE -- whatever was not reached is still queued.
That is the whole point of the redesign, so it is the first thing pinned.
"""
from mcpbrain.store import Store
from mcpbrain.sync.queue import work_queue

NOW = "2026-09-04T10:00:00"


def _store(tmp_path):
    s = Store(tmp_path / "w.sqlite3", dim=4)
    s.init()
    return s


def _seed(s, refs, source="drive"):
    s.enqueue_and_advance(
        [{"ref_id": r, "version": "1", "event": "upsert",
          "modified_at": f"2026-09-{i + 1:02d}T00:00:00"} for i, r in enumerate(refs)],
        source=source, cursor="1")


class _StopBudget:
    def __init__(self, allow): self._left = allow
    def expired(self):
        if self._left > 0:
            self._left -= 1
            return False
        return True


def test_successful_items_leave_the_queue(tmp_path):
    s = _store(tmp_path); _seed(s, ["a", "b"])
    seen = []
    out = work_queue(s, handlers={"drive": seen.append}, limit=10, now=NOW)
    assert out == {"processed": 2, "failed": 0}
    assert s.sync_queue_pending() == 0
    assert [i["ref_id"] for i in seen] == ["b", "a"]      # newest-first


def test_budget_cutoff_leaves_the_rest_queued(tmp_path):
    """The property the whole redesign exists for: a cutoff loses nothing."""
    s = _store(tmp_path); _seed(s, ["a", "b", "c"])
    out = work_queue(s, handlers={"drive": lambda i: None}, limit=10,
                     budget=_StopBudget(1), now=NOW)
    assert out["processed"] == 1
    assert s.sync_queue_pending() == 2


def test_a_failing_item_backs_off_and_the_loop_continues(tmp_path):
    s = _store(tmp_path); _seed(s, ["good", "bad"])

    def handler(item):
        if item["ref_id"] == "bad":
            raise RuntimeError("export timeout")

    out = work_queue(s, handlers={"drive": handler}, limit=10, now=NOW)
    assert out == {"processed": 1, "failed": 1}
    assert s.sync_queue_pending() == 1                    # 'bad' retained
    assert s.due_sync_items(limit=10, now=NOW) == []      # and backed off


def test_shared_drive_source_resolves_to_the_drive_handler(tmp_path):
    s = _store(tmp_path); _seed(s, ["x"], source="drive:0ABC")
    seen = []
    work_queue(s, handlers={"drive": seen.append}, limit=10, now=NOW)
    assert [i["ref_id"] for i in seen] == ["x"]


def test_a_crash_after_the_write_leaves_the_item_queued(tmp_path):
    """At-least-once delivery: the item is re-worked, and that is SAFE because
    the handlers are idempotent. This is the property the spec rests on -- the
    row deletion is NOT in the handler's transaction."""
    s = _store(tmp_path); _seed(s, ["a"])
    writes = []

    def handler(item):
        writes.append(item["ref_id"])       # stands in for the chunk write
        raise RuntimeError("crash after write, before completion")

    work_queue(s, handlers={"drive": handler}, limit=10, now=NOW)
    assert s.sync_queue_pending() == 1, "item lost after a post-write crash"

    # Next cycle (past the backoff) re-works it; a converging handler completes.
    work_queue(s, handlers={"drive": lambda i: writes.append(i["ref_id"])},
               limit=10, now="2026-09-04T10:05:00")
    assert writes == ["a", "a"], "the item was not re-worked"
    assert s.sync_queue_pending() == 0


def test_an_unknown_source_fails_the_item_rather_than_the_loop(tmp_path):
    s = _store(tmp_path); _seed(s, ["x"], source="mystery")
    out = work_queue(s, handlers={"drive": lambda i: None}, limit=10, now=NOW)
    assert out == {"processed": 0, "failed": 1}
    assert s.sync_queue_pending() == 1
