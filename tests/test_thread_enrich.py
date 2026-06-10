"""Tests for thread_enrich — grouping the unenriched backlog by thread and
reassembling a thread's chunks into ordered messages.

These primitives are consumed by prepare.py's _group_unenriched_threads /
_reassemble_thread seams. The interface is locked: group_unenriched_threads
takes the keyword thread_cap, returns batch objects with .thread_id / .doc_ids /
.chunks; reassemble_thread returns message dicts with the per-message provenance
fields plus the body text.
"""

from mcpbrain.store import Store
from mcpbrain import thread_enrich


def _store(tmp_path):
    s = Store(tmp_path / "t.sqlite3", dim=4)
    s.init()
    return s


def _seed(store, doc_id, *, text="body", thread_id="", message_id="",
          chunk_index=0, sender="", subject="", date="", labels=""):
    """Insert one unenriched chunk (enriched defaults to 0)."""
    meta = {
        "source_type": "gmail",
        "message_id": message_id,
        "thread_id": thread_id,
        "subject": subject,
        "sender": sender,
        "date": date,
        "labels": labels,
        "content_type": "email_body",
        "chunk_index": chunk_index,
    }
    store.upsert_chunk(doc_id, text, f"hash-{doc_id}", meta)


def test_group_unenriched_by_thread(tmp_path):
    store = _store(tmp_path)
    # Two threads, two chunks each.
    _seed(store, "gmail-a-body-0", thread_id="thread-A", message_id="a", chunk_index=0)
    _seed(store, "gmail-a-body-1", thread_id="thread-A", message_id="a", chunk_index=1)
    _seed(store, "gmail-b-body-0", thread_id="thread-B", message_id="b", chunk_index=0)
    _seed(store, "gmail-b-body-1", thread_id="thread-B", message_id="b", chunk_index=1)

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=10)

    by_id = {b.thread_id: b for b in batches}
    assert set(by_id) == {"thread-A", "thread-B"}
    assert set(by_id["thread-A"].doc_ids) == {"gmail-a-body-0", "gmail-a-body-1"}
    assert set(by_id["thread-B"].doc_ids) == {"gmail-b-body-0", "gmail-b-body-1"}
    # .chunks are the raw chunk dicts (carry the parsed metadata).
    assert len(by_id["thread-A"].chunks) == 2
    assert all("metadata" in c for c in by_id["thread-A"].chunks)


def test_group_missing_thread_id_falls_back_to_message_then_doc(tmp_path):
    store = _store(tmp_path)
    # No thread_id, but a message_id -> singleton keyed on message_id.
    _seed(store, "gmail-m1-body-0", thread_id="", message_id="m1", chunk_index=0)
    # No thread_id and no message_id -> singleton keyed on doc_id.
    _seed(store, "gmail-m2-body-0", thread_id="", message_id="", chunk_index=0)

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=10)
    keys = {b.thread_id for b in batches}
    assert keys == {"m1", "gmail-m2-body-0"}
    # Each is a singleton.
    assert all(len(b.doc_ids) == 1 for b in batches)


def test_thread_cap_limits_threads(tmp_path):
    store = _store(tmp_path)
    # 5 distinct threads, one chunk each. Seed in a deterministic order so the
    # cap is stable: group_unenriched_threads preserves first-appearance order,
    # which mirrors the rowid order unenriched_chunks returns.
    for i in range(5):
        _seed(store, f"gmail-t{i}-body-0", thread_id=f"thread-{i}",
              message_id=f"t{i}", chunk_index=0)

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=2)
    # The cap counts THREADS, not chunks. First two by newest-synced order (rowid DESC).
    assert len(batches) == 2
    assert [b.thread_id for b in batches] == ["thread-4", "thread-3"]


def test_reassemble_thread_orders_messages_by_date(tmp_path):
    store = _store(tmp_path)
    # One thread, two messages, two body chunks each. m-late is seeded first to
    # prove ordering is by date, not insertion order.
    _seed(store, "gmail-late-body-0", thread_id="thread-X", message_id="m-late",
          chunk_index=0, text="late part one", sender="b@x.com",
          subject="Re: hello", date="2026-06-02T10:00:00Z", labels="INBOX")
    _seed(store, "gmail-late-body-1", thread_id="thread-X", message_id="m-late",
          chunk_index=1, text="late part two", sender="b@x.com",
          subject="Re: hello", date="2026-06-02T10:00:00Z", labels="INBOX")
    _seed(store, "gmail-early-body-0", thread_id="thread-X", message_id="m-early",
          chunk_index=0, text="early part one", sender="a@x.com",
          subject="hello", date="2026-06-01T09:00:00Z", labels="INBOX,IMPORTANT")
    _seed(store, "gmail-early-body-1", thread_id="thread-X", message_id="m-early",
          chunk_index=1, text="early part two", sender="a@x.com",
          subject="hello", date="2026-06-01T09:00:00Z", labels="INBOX,IMPORTANT")

    batch = thread_enrich.group_unenriched_threads(store, thread_cap=10)[0]
    messages = thread_enrich.reassemble_thread(batch.chunks)

    assert [m["message_id"] for m in messages] == ["m-early", "m-late"]
    early = messages[0]
    assert early["sender"] == "a@x.com"
    assert early["subject"] == "hello"
    assert early["date"] == "2026-06-01T09:00:00Z"
    assert early["labels"] == "INBOX,IMPORTANT"
    # Body chunks concatenated in chunk_index order with a blank line.
    assert early["text"] == "early part one\n\nearly part two"
    assert messages[1]["text"] == "late part one\n\nlate part two"
    # Provenance contract: every message carries the locked fields.
    for m in messages:
        assert set(m) >= {"message_id", "sender", "date", "labels", "subject", "text"}


def test_reassemble_orders_chunks_out_of_order(tmp_path):
    store = _store(tmp_path)
    # Chunk 1 seeded before chunk 0 -> reassemble must sort by chunk_index.
    _seed(store, "gmail-m-body-1", thread_id="thread-Y", message_id="m",
          chunk_index=1, text="second", date="2026-06-01T00:00:00Z")
    _seed(store, "gmail-m-body-0", thread_id="thread-Y", message_id="m",
          chunk_index=0, text="first", date="2026-06-01T00:00:00Z")

    batch = thread_enrich.group_unenriched_threads(store, thread_cap=10)[0]
    messages = thread_enrich.reassemble_thread(batch.chunks)
    assert len(messages) == 1
    assert messages[0]["text"] == "first\n\nsecond"


def test_reassemble_thread_empty_returns_empty(tmp_path):
    # No chunks in, no messages out.
    assert thread_enrich.reassemble_thread([]) == []


def test_group_unenriched_thread_cap_zero_returns_empty(tmp_path):
    store = _store(tmp_path)
    # A backlog of several threads, but a zero cap admits none of them.
    for i in range(3):
        _seed(store, f"gmail-t{i}-body-0", thread_id=f"thread-{i}",
              message_id=f"t{i}", chunk_index=0)

    batches = thread_enrich.group_unenriched_threads(store, thread_cap=0)
    assert batches == []
