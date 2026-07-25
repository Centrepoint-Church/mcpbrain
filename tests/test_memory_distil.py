"""memory_distil: expire/merge memory notes; promote candidates -> findings."""
from datetime import datetime, timedelta, timezone

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


def test_promotion_finding_carries_org(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-org", text="Prefers tables\n\nbody",
                   content_hash="note-org",
                   metadata={"source": "note", "title": "Prefers tables",
                             "observation_type": "memory", "org": "Acme",
                             "captured_at": "2026-06-01T00:00:00Z"})
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-org", "verdict": "promote",
         "reason": "stated 4 times", "target_hint": "preferences.md"},
    ]})
    finds = s.open_findings("memory_promotion")
    assert finds and finds[0]["org"] == "Acme"


def test_unknown_doc_or_verdict_skipped(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "T")
    n = memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "ghost", "verdict": "expire"},
        {"doc_id": "note-a", "verdict": "vaporise"},
    ]})
    assert n == {"expired": 0, "promotions_flagged": 0}


def test_drain_stamps_distilled_at_on_keep(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "Keep me")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-a", "verdict": "keep"},
    ]})
    chunk = s.get_chunk("note-a")
    assert chunk["metadata"].get("distilled_at")


def test_drain_stamps_distilled_at_on_expire(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-b", "Expire me")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-b", "verdict": "expire", "reason": "stale"},
    ]})
    chunk = s.get_chunk("note-b")
    assert chunk["metadata"].get("distilled_at")
    assert chunk["metadata"].get("expired") is True


def test_drain_stamps_distilled_at_on_promote(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-c", "Promote me")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-c", "verdict": "promote",
         "reason": "stated 4 times", "target_hint": "preferences.md"},
    ]})
    chunk = s.get_chunk("note-c")
    assert chunk["metadata"].get("distilled_at")


def test_build_distil_requests_excludes_already_distilled_notes(tmp_path):
    s = _store(tmp_path)
    _note(s, "note-a", "Already distilled")
    _note(s, "note-b", "Fresh note")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-a", "verdict": "keep"},
    ]})

    reqs = memory_distil.build_distil_requests(s, cap=30)

    assert {r["doc_id"] for r in reqs} == {"note-b"}


def test_drain_stamps_distilled_verdict_alongside_distilled_at(tmp_path):
    """distilled_verdict must record WHICH verdict produced the stamp, so
    note_chunks can tell a deferral (keep) apart from a decision
    (expire/promote) when deciding whether to re-include it."""
    s = _store(tmp_path)
    _note(s, "note-k", "Keep me")
    _note(s, "note-e", "Expire me")
    _note(s, "note-p", "Promote me")
    memory_distil.drain_distil(s, {"memory_distil": [
        {"doc_id": "note-k", "verdict": "keep"},
        {"doc_id": "note-e", "verdict": "expire", "reason": "stale"},
        {"doc_id": "note-p", "verdict": "promote",
         "reason": "stated 4 times", "target_hint": "preferences.md"},
    ]})

    assert s.get_chunk("note-k")["metadata"]["distilled_verdict"] == "keep"
    assert s.get_chunk("note-e")["metadata"]["distilled_verdict"] == "expire"
    assert s.get_chunk("note-p")["metadata"]["distilled_verdict"] == "promote"


def test_build_distil_requests_resurfaces_stale_keep_note(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-stale", text="Stale\n\nbody", content_hash="note-stale",
                   metadata={"source": "note", "title": "Stale",
                             "observation_type": "memory",
                             "captured_at": "2026-05-01T00:00:00Z",
                             "distilled_at": "2026-06-01T00:00:00Z",
                             "distilled_verdict": "keep"})

    reqs = memory_distil.build_distil_requests(s, cap=30, keep_review_days=30)

    assert {r["doc_id"] for r in reqs} == {"note-stale"}


def test_build_distil_requests_leaves_fresh_keep_note_excluded(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk(doc_id="note-fresh", text="Fresh\n\nbody", content_hash="note-fresh",
                   metadata={"source": "note", "title": "Fresh",
                             "observation_type": "memory",
                             "captured_at": "2026-07-20T00:00:00Z",
                             "distilled_at": (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%dT%H:%M:%SZ"),
                             "distilled_verdict": "keep"})

    reqs = memory_distil.build_distil_requests(s, cap=30, keep_review_days=30)

    assert reqs == []
