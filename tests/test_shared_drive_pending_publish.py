"""shared_drive_pending_publish: the durable seam between a cache-miss
extraction and the fleet-cache publish step.

Today this is an in-memory (file_id, content_hash) list threaded through one
synchronous sync->embed->publish call. The queue model works items
asynchronously per-cycle, so there is no such list left over after
work_queue returns -- this table is what survives across cycles instead.
"""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "p.sqlite3", dim=4)
    s.init()
    return s


def test_record_and_list_pending_publishes(tmp_path):
    s = _store(tmp_path)
    s.record_pending_publish("D1", "f1", "hash1")
    s.record_pending_publish("D1", "f2", "hash2")
    s.record_pending_publish("D2", "f3", "hash3")
    assert sorted(s.pending_publishes("D1")) == [("f1", "hash1"), ("f2", "hash2")]
    assert s.pending_publishes("D2") == [("f3", "hash3")]
    assert s.pending_publishes("D3") == []


def test_new_content_hash_replaces_the_old_pending_row(tmp_path):
    """The file changed again before its old miss was published -- only the
    latest version should ever reach the fleet cache."""
    s = _store(tmp_path)
    s.record_pending_publish("D1", "f1", "hash1")
    s.record_pending_publish("D1", "f1", "hash2")
    assert s.pending_publishes("D1") == [("f1", "hash2")]


def test_clear_removes_one_entry_only(tmp_path):
    s = _store(tmp_path)
    s.record_pending_publish("D1", "f1", "hash1")
    s.record_pending_publish("D1", "f2", "hash2")
    s.clear_pending_publish("D1", "f1")
    assert s.pending_publishes("D1") == [("f2", "hash2")]
