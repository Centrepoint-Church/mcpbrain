"""The per-cycle publish-pending step: only clears a pending-publish row
once THAT file's publish genuinely succeeded -- a chunk that isn't embedded
yet must stay pending and retry next cycle, not be silently dropped.
"""
from mcpbrain.store import Store
from mcpbrain.sync import publish_pending_shared_drive_artifacts
from mcpbrain.org_contracts import FleetPin

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
              enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path):
    s = Store(tmp_path / "p.sqlite3", dim=4)
    s.init()
    return s


class _FakeIngestCache:
    """publish_file succeeds for files with 'ready' in their content_hash,
    and no-ops (returns False) for anything else -- simulating an
    unembedded chunk."""
    def __init__(self):
        self.published = []

    def publish_file(self, store, fs, drive_id, file_id, content_hash, pin,
                     **kw):
        if "ready" in content_hash:
            self.published.append((drive_id, file_id, content_hash))
            return True
        return False


def test_only_successfully_published_files_are_cleared(tmp_path):
    s = _store(tmp_path)
    s.record_pending_publish("D1", "f1", "hash-ready-1")
    s.record_pending_publish("D1", "f2", "hash-not-embedded-yet")
    ic = _FakeIngestCache()
    out = publish_pending_shared_drive_artifacts(
        s, ic, drives_fs={"D1": object()}, pin=PIN, published_by="a@b.c")
    assert out == {"D1": 1}
    assert s.pending_publishes("D1") == [("f2", "hash-not-embedded-yet")]
    assert ("D1", "f1", "hash-ready-1") in ic.published


def test_empty_pending_set_is_a_no_op(tmp_path):
    s = _store(tmp_path)
    ic = _FakeIngestCache()
    out = publish_pending_shared_drive_artifacts(
        s, ic, drives_fs={"D1": object()}, pin=PIN, published_by="a@b.c")
    assert out == {"D1": 0}


def test_budget_cutoff_leaves_the_rest_pending(tmp_path):
    """A cutoff here must be free -- unpublished rows stay pending, retried
    next cycle. Mirrors the same property work_queue itself guarantees."""
    s = _store(tmp_path)
    s.record_pending_publish("D1", "f1", "hash-ready-1")
    s.record_pending_publish("D1", "f2", "hash-ready-2")
    ic = _FakeIngestCache()

    class _ExpireAfterOne:
        def __init__(self): self._n = 0
        def expired(self):
            self._n += 1
            return self._n > 1

    out = publish_pending_shared_drive_artifacts(
        s, ic, drives_fs={"D1": object()}, pin=PIN, published_by="a@b.c",
        budget=_ExpireAfterOne())
    assert out == {"D1": 1}
    assert len(s.pending_publishes("D1")) == 1, "the un-reached file must stay pending"
