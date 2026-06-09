"""Tests for the stale -> re-extraction trigger sweep (Gap A)."""
from mcpbrain import stale_reextract
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _chunk(s, doc_id, thread_id, text, chash, *, enriched, date):
    s.upsert_chunk(doc_id, text, chash, {"thread_id": thread_id, "date": date})
    with s._connect() as db:
        db.execute("UPDATE chunks SET enriched=? WHERE doc_id=?", (enriched, doc_id))


def _stale_thread(s, thread_id, *, enriched):
    # source message (older) + a later message carrying a resolution marker.
    _chunk(s, f"{thread_id}-src", thread_id, "Please send the report",
           f"{thread_id}h1", enriched=enriched,
           date="Mon, 01 Jun 2026 09:00:00 +0000")
    _chunk(s, f"{thread_id}-rep", thread_id, "All done, sent it through",
           f"{thread_id}h2", enriched=enriched,
           date="Tue, 02 Jun 2026 09:00:00 +0000")
    return s.add_unified_action(
        text="Send the report", owner="Joshua", status="open",
        source_doc_id=f"{thread_id}-src", thread_id=thread_id)


def test_sweep_triggers_idle_stale_thread(tmp_path):
    s = _store(tmp_path)
    _stale_thread(s, "T1", enriched=1)        # idle + stale -> candidate
    out = stale_reextract.sweep(s, now="2026-06-09T00:00:00Z")
    assert out["triggered"] == 1
    assert "T1" in out["threads"]
    assert s.thread_has_unenriched("T1") is True          # reset for re-extract
    assert s.get_stale_reextract("T1") is not None         # marker recorded


def test_sweep_skips_thread_with_pending_chunks(tmp_path):
    s = _store(tmp_path)
    _stale_thread(s, "T2", enriched=0)        # already has unenriched chunks
    out = stale_reextract.sweep(s, now="2026-06-09T00:00:00Z")
    assert out["triggered"] == 0


def test_sweep_loop_guard_same_state(tmp_path):
    s = _store(tmp_path)
    _stale_thread(s, "T3", enriched=1)
    first = stale_reextract.sweep(s, now="2026-06-09T00:00:00Z")
    assert first["triggered"] == 1
    # Simulate the normal enrichment cycle having re-enriched the thread
    # (content unchanged) so it's idle again at the SAME signature.
    s.mark_enriched(["T3-src", "T3-rep"])
    second = stale_reextract.sweep(s, now="2026-06-09T02:00:00Z")
    assert second["triggered"] == 0          # not re-triggered at same state


def test_sweep_rearms_after_content_change(tmp_path):
    s = _store(tmp_path)
    _stale_thread(s, "T4", enriched=1)
    stale_reextract.sweep(s, now="2026-06-09T00:00:00Z")
    s.mark_enriched(["T4-src", "T4-rep"])
    # New message arrives in the thread (content + signature change), idle again.
    _chunk(s, "T4-rep2", "T4", "still all done", "T4h3", enriched=1,
           date="Wed, 03 Jun 2026 09:00:00 +0000")
    out = stale_reextract.sweep(s, now="2026-06-09T03:00:00Z")
    assert out["triggered"] == 1             # re-armed by the content change


def test_sweep_respects_cap_and_reports_deferred(tmp_path):
    s = _store(tmp_path)
    for i in range(3):
        _stale_thread(s, f"C{i}", enriched=1)
    out = stale_reextract.sweep(s, now="2026-06-09T00:00:00Z", cap=2)
    assert out["triggered"] == 2
    assert out["deferred"] == 1
