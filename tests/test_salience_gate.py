"""Tests for Q1 salience gate (should_enrich + _apply_salience_gate + cold state)."""
import pytest
from mcpbrain.prepare import should_enrich, _apply_salience_gate
from mcpbrain.thread_enrich import ThreadBatch


# ---------------------------------------------------------------------------
# should_enrich unit tests
# ---------------------------------------------------------------------------

def _chunk(text="hello", meta=None):
    return {"doc_id": "x", "text": text, "metadata": meta or {}}


class TestShouldEnrichEmail:
    def test_normal_email_passes(self):
        c = _chunk(meta={"source": "gmail", "labels": "INBOX"})
        assert should_enrich(c) is True

    def test_promotions_label_gated(self):
        c = _chunk(meta={"source": "gmail", "labels": "CATEGORY_PROMOTIONS,INBOX"})
        assert should_enrich(c) is False

    def test_updates_label_NOT_gated(self):
        # CATEGORY_UPDATES is intentionally NOT skipped (legit threads land there;
        # deprioritising it is B3's job, not this binary gate).
        c = _chunk(meta={"labels": ["CATEGORY_UPDATES"], "thread_id": "t1"})
        assert should_enrich(c) is True

    def test_social_label_gated(self):
        c = _chunk(meta={"source": "gmail", "labels": "CATEGORY_SOCIAL"})
        assert should_enrich(c) is False

    def test_label_case_insensitive(self):
        c = _chunk(meta={"source": "gmail", "labels": "category_promotions"})
        assert should_enrich(c) is False

    def test_no_labels_passes(self):
        c = _chunk(meta={"source": "gmail"})
        assert should_enrich(c) is True

    def test_source_type_key_is_used(self):
        # The real metadata field is `source_type` (not `source`) — the primary
        # branch must key on it, not rely on the thread_id fallback.
        c = _chunk(meta={"source_type": "gmail", "labels": "CATEGORY_PROMOTIONS"})
        assert should_enrich(c) is False


class TestShouldEnrichDrive:
    def test_spreadsheet_gated(self):
        c = _chunk("x", meta={"source": "gdrive",
                               "mime_type": "application/vnd.google-apps.spreadsheet"})
        assert should_enrich(c) is False

    def test_csv_gated(self):
        c = _chunk("x,y,z\n1,2,3\n", meta={"mime_type": "text/csv", "file_id": "abc"})
        assert should_enrich(c) is False

    def test_xlsx_gated(self):
        c = _chunk("x", meta={
            "source": "drive",
            "mime_type": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        })
        assert should_enrich(c) is False

    def test_short_doc_gated(self):
        short = "a" * 50  # below _MIN_DRIVE_TEXT=200
        c = _chunk(short, meta={"source": "gdrive", "mime_type": "application/pdf"})
        assert should_enrich(c) is False

    def test_long_doc_passes(self):
        long_text = "word " * 100  # 500 chars
        c = _chunk(long_text, meta={"source": "gdrive", "mime_type": "application/pdf"})
        assert should_enrich(c) is True

    def test_google_doc_passes(self):
        long_text = "content " * 50
        c = _chunk(long_text, meta={"source": "gdrive",
                                    "mime_type": "application/vnd.google-apps.document"})
        assert should_enrich(c) is True

    def test_source_type_key_is_used(self):
        # Primary branch keys on `source_type` (the real field), not just file_id.
        c = _chunk("x", meta={"source_type": "gdrive", "mime_type": "text/csv"})
        assert should_enrich(c) is False


class TestShouldEnrichUnknown:
    def test_unknown_source_passes(self):
        """Unknown source: fail-open, always passes."""
        c = _chunk("some text", meta={"source": "calendar"})
        assert should_enrich(c) is True

    def test_empty_meta_passes(self):
        c = _chunk("hello", meta={})
        assert should_enrich(c) is True


# ---------------------------------------------------------------------------
# _apply_salience_gate integration tests
# ---------------------------------------------------------------------------

@pytest.fixture
def store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "t.sqlite3", dim=4)
    s.init()
    return s


def _batch(thread_id, chunks):
    b = ThreadBatch(thread_id=thread_id)
    b.chunks = chunks
    b.doc_ids = [c["doc_id"] for c in chunks]
    return b


def test_gate_marks_cold_chunks(store):
    """Cold chunks get enrich_state='cold' and are excluded from returned batches."""
    cold = {"doc_id": "cold-1", "text": "x",
            "metadata": {"source": "gdrive", "mime_type": "text/csv"}}
    warm = {"doc_id": "warm-1", "text": "long enough prose " * 20,
            "metadata": {"source": "gdrive", "mime_type": "application/pdf"}}
    for c in [cold, warm]:
        store.upsert_chunk(c["doc_id"], c["text"], "h", {})
    batch = _batch("t1", [cold, warm])
    kept_batches, summary = _apply_salience_gate(store, [batch])

    assert summary["gated"] == 1
    assert summary["kept"] == 1
    assert len(kept_batches) == 1
    assert kept_batches[0].doc_ids == ["warm-1"]

    # Verify DB state.
    assert store.cold_chunk_count() == 1


def test_gate_empty_batch_discarded(store):
    """A batch where all chunks are gated is dropped entirely."""
    cold = {"doc_id": "c1", "text": "x",
            "metadata": {"source": "gmail", "labels": "CATEGORY_PROMOTIONS"}}
    store.upsert_chunk("c1", "x", "h", {})
    batch = _batch("t1", [cold])
    kept, summary = _apply_salience_gate(store, [batch])
    assert kept == []
    assert summary["gated"] == 1


def test_cold_chunks_excluded_from_unenriched(store):
    """unenriched_chunks() must not return cold-state chunks."""
    store.upsert_chunk("e1", "embeddable text", "h1", {})
    store.upsert_chunk("cold1", "cold text", "h2", {})
    store.set_enrich_state(["cold1"], "cold")

    unenriched = store.unenriched_chunks()
    ids = {c["doc_id"] for c in unenriched}
    assert "e1" in ids
    assert "cold1" not in ids


def test_cold_state_reversible(store):
    """Resetting enrich_state='' re-admits a cold chunk to the backlog."""
    store.upsert_chunk("r1", "text", "h", {})
    store.set_enrich_state(["r1"], "cold")
    assert store.cold_chunk_count() == 1

    store.set_enrich_state(["r1"], "")
    assert store.cold_chunk_count() == 0
    ids = {c["doc_id"] for c in store.unenriched_chunks()}
    assert "r1" in ids


def test_require_drive_mention_gates_unmentioned_prose(store):
    """With require_drive_mention=True, a prose Drive doc not referenced in any
    email is cold-gated; one whose file_id is mentioned in an email is kept."""
    prose = "long enough prose " * 20
    # An email that mentions only fileAAA's id.
    store.upsert_chunk("gmail-1-body-0", "see the doc at drive id fileAAA please",
                       "he", {"source_type": "gmail"})
    mentioned = {"doc_id": "gdrive-fileAAA-0", "text": prose,
                 "metadata": {"source_type": "gdrive", "mime_type": "application/pdf",
                              "file_id": "fileAAA", "file_name": "Plan.pdf"}}
    orphan = {"doc_id": "gdrive-fileBBB-0", "text": prose,
              "metadata": {"source_type": "gdrive", "mime_type": "application/pdf",
                           "file_id": "fileBBB", "file_name": "Orphan.pdf"}}
    for c in (mentioned, orphan):
        store.upsert_chunk(c["doc_id"], c["text"], "h", {})
    kept, summary = _apply_salience_gate(store, [_batch("t1", [mentioned, orphan])],
                                         require_drive_mention=True)
    assert summary["kept"] == 1 and summary["gated"] == 1
    assert kept[0].doc_ids == ["gdrive-fileAAA-0"]
    # Without the strict flag, BOTH prose docs pass (default behaviour).
    store.set_enrich_state(["gdrive-fileAAA-0", "gdrive-fileBBB-0"], "")
    kept2, summary2 = _apply_salience_gate(store, [_batch("t2", [mentioned, orphan])])
    assert summary2["kept"] == 2
