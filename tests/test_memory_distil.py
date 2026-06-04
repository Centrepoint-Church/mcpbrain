"""memory_distil: expire/merge memory notes; promote candidates -> findings."""
from mcpbrain import memory_distil
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _note(s, doc_id, title):
    s.upsert_chunk(doc_id=doc_id, text=f"{title}\n\nbody", content_hash=doc_id,
                   metadata={"source": "note", "title": title,
                             "observation_type": "memory",
                             "captured_at": "2026-06-01T00:00:00Z"})


def test_requests_list_live_memories(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "Prefers tables")
    reqs = memory_distil.build_distil_requests(s, cap=30)
    assert reqs[0]["doc_id"] == "note-a"
    assert {"doc_id", "title", "content", "captured_at"} <= set(reqs[0])


def test_drain_expires_and_promotes(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "Dup one")
    _note(s, "note-b", "Dup two")
    _note(s, "note-c", "Recurring preference")
    n = memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-a", "verdict": "keep"},
        {"doc_id": "note-b", "verdict": "expire", "reason": "duplicate of note-a"},
        {"doc_id": "note-c", "verdict": "promote",
         "reason": "stated 4 times", "target_hint": "preferences.md"},
    ]})
    assert n["expired"] == 1 and n["promotions_flagged"] == 1
    live = {c["doc_id"] for c in s.note_chunks(observation_type="memory")}
    assert live == {"note-a", "note-c"}     # promote keeps the note live
    finds = s.open_findings("memory_promotion")
    assert finds and finds[0]["ref_id"] == "note-c"
    changes = {c["change_type"] for c in s.recent_changes(10)}
    assert "memory_expired" in changes


def test_unknown_doc_or_verdict_skipped(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "T")
    n = memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "ghost", "verdict": "expire"},
        {"doc_id": "note-a", "verdict": "vaporise"},
    ]})
    assert n == {"expired": 0, "promotions_flagged": 0}
