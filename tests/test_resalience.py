import sqlite3
from bin import resalience


class FakeStore:
    def __init__(self, rows):
        self.rows = rows
        self.cold = []

    def iter_hot_chunks(self):
        return iter(self.rows)

    def set_enrich_state(self, doc_ids, state):
        assert state == "cold"
        self.cold.extend(doc_ids)


def test_scan_finds_chunks_that_now_fail_the_gate():
    rows = [
        {"doc_id": "a", "text": "x" * 5000,
         "metadata": {"source_type": "gdrive", "file_id": "f", "mime_type": "text/html"}},
        {"doc_id": "b", "text": "x" * 5000,
         "metadata": {"source_type": "gdrive", "file_id": "g", "mime_type": "application/pdf"}},
    ]
    assert resalience.scan(FakeStore(rows)) == ["a"]


def test_apply_cold_marks_and_returns_count():
    store = FakeStore([])
    assert resalience.apply(store, ["a", "b"]) == 2
    assert store.cold == ["a", "b"]


def test_apply_is_a_noop_on_empty():
    store = FakeStore([])
    assert resalience.apply(store, []) == 0
    assert store.cold == []
