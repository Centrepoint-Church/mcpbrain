"""Tests for per-source sync cursors — advance-after-durable-write semantics."""
import pytest

from mcpbrain.store import Store
from mcpbrain.sync.cursors import advance_after, get_cursor, set_cursor


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_get_cursor_absent_returns_none(store):
    assert get_cursor(store, "gmail") is None


def test_set_then_get_roundtrips_and_overwrites(store):
    set_cursor(store, "gmail", "100")
    assert get_cursor(store, "gmail") == "100"

    set_cursor(store, "gmail", "200")
    assert get_cursor(store, "gmail") == "200"


def test_cursor_persists_across_reopen(store, tmp_path):
    set_cursor(store, "gmail", "abc123")

    store2 = Store(tmp_path / "b.sqlite3", dim=4)
    store2.init()
    assert get_cursor(store2, "gmail") == "abc123"


def test_advance_after_advances_on_success(store):
    import hashlib

    def write_batch():
        store.upsert_chunk(
            doc_id="chunk-1",
            text="hello world",
            content_hash=hashlib.sha256(b"hello world").hexdigest(),
            metadata={"source": "gmail"},
        )

    advance_after(store, "gmail", "300", write_batch)

    assert get_cursor(store, "gmail") == "300"
    chunk = store.get_chunk("chunk-1")
    assert chunk is not None
    assert chunk["text"] == "hello world"


def test_advance_after_leaves_cursor_on_failure(store):
    set_cursor(store, "gmail", "300")

    def failing_write():
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError, match="disk full"):
        advance_after(store, "gmail", "999", failing_write)

    assert get_cursor(store, "gmail") == "300"
