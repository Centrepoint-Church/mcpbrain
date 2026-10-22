# tests/test_duplicate_carry.py
"""Spec 2026-10-22 §4: a chunk whose text equals an already-enriched evidence
chunk's is carried (dup_of, enriched=1, enrich_state='duplicate') instead of
being extracted again."""
from mcpbrain.chunking import content_hash
from mcpbrain.store import Store

BODY = ("Quarterly facilities review: the north wing roof leak was patched on "
        "Tuesday, the contractor invoice is due on the 30th, and the fire "
        "warden roster needs two more volunteers before the inspection. ") * 2


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def _mail(thread, **extra):
    return {"source_type": "gmail", "thread_id": thread, "message_id": f"m-{thread}",
            **extra}


def _put(s, doc_id, text, meta):
    return s.upsert_chunk(doc_id, text, content_hash(text), meta)


def _row(s, doc_id):
    with s._connect() as db:
        r = db.execute("SELECT enriched, enrich_state, dup_of FROM chunks "
                       "WHERE doc_id=?", (doc_id,)).fetchone()
    return (r["enriched"], r["enrich_state"] or "", r["dup_of"]) if r else None


def _unenriched_ids(s):
    return {c["doc_id"] for c in s.unenriched_chunks()}


def test_identical_text_marks_dup_and_skips_extraction(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    s.mark_enriched(["gm-a"])
    _put(s, "gm-b", BODY, _mail("t2"))
    assert _row(s, "gm-b") == (1, "duplicate", "gm-a")
    assert "gm-b" not in _unenriched_ids(s)
    assert s.canonical_for("gm-b") == "gm-a"
    assert s.canonical_for("gm-a") == "gm-a"
    assert s.canonical_for("missing") == "missing"


def test_short_chunks_not_carried(tmp_path):
    s = _store(tmp_path)
    short = "Thanks, see you Sunday."
    _put(s, "gm-a", short, _mail("t1"))
    s.mark_enriched(["gm-a"])
    _put(s, "gm-b", short, _mail("t2"))
    assert _row(s, "gm-b") == (0, "", None)


def test_changed_canonical_does_not_orphan_dup(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    s.mark_enriched(["gm-a"])
    _put(s, "gm-b", BODY, _mail("t2"))
    assert _row(s, "gm-b")[2] == "gm-a"
    _put(s, "gm-a", BODY + " Edited.", _mail("t1"))
    assert _row(s, "gm-b") == (0, "", None)
    assert "gm-b" in _unenriched_ids(s)


def test_deleted_canonical_releases_dup(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    s.mark_enriched(["gm-a"])
    _put(s, "gm-b", BODY, _mail("t2"))
    s.delete_chunks(["gm-a"])
    assert _row(s, "gm-b") == (0, "", None)


def test_unenriched_canonical_not_carried(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    _put(s, "gm-b", BODY, _mail("t2"))
    assert _row(s, "gm-b") == (0, "", None)


def test_cold_canonical_not_carried(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    s.mark_enriched(["gm-a"])
    s.set_enrich_state(["gm-a"], "cold")
    _put(s, "gm-b", BODY, _mail("t2"))
    assert _row(s, "gm-b")[2] is None


def test_derived_canonical_not_carried(tmp_path):
    """A primary message is never made a duplicate of a draft/derived copy."""
    s = _store(tmp_path)
    _put(s, "gm-draft", BODY, _mail("t1", labels="DRAFT"))
    s.mark_enriched(["gm-draft"])
    _put(s, "note-consolidated-x", BODY, {"source": "note"})
    s.mark_enriched(["note-consolidated-x"])
    _put(s, "gm-sent", BODY, _mail("t2", labels="SENT"))
    assert _row(s, "gm-sent") == (0, "", None)


def test_derived_new_chunk_not_marked_duplicate(tmp_path):
    """A draft copy of a primary message keeps its own (derived) class."""
    s = _store(tmp_path)
    _put(s, "gm-sent", BODY, _mail("t1", labels="SENT"))
    s.mark_enriched(["gm-sent"])
    _put(s, "gm-draft", BODY, _mail("t2", labels="DRAFT"))
    assert _row(s, "gm-draft")[2] is None


def test_oldest_enriched_evidence_is_canonical(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    _put(s, "gm-b", BODY, _mail("t2"))
    s.mark_enriched(["gm-a", "gm-b"])
    _put(s, "gm-c", BODY, _mail("t3"))
    assert _row(s, "gm-c")[2] == "gm-a"


def test_dup_never_chains(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    s.mark_enriched(["gm-a"])
    _put(s, "gm-b", BODY, _mail("t2"))
    s.delete_chunks(["gm-a"])        # gm-b released, enriched=0
    _put(s, "gm-c", BODY, _mail("t3"))
    assert _row(s, "gm-c")[2] is None  # gm-b is not enriched: nothing to carry


def test_version_reflow_does_not_reset_dup(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    s.mark_enriched(["gm-a"])
    _put(s, "gm-b", BODY, _mail("t2"))
    s.reflow_outdated_chunks(version=10**6, cap=100)
    assert _row(s, "gm-b") == (1, "duplicate", "gm-a")


def test_content_hash_lookup_is_indexed(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        plan = " ".join(r[-1] for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT doc_id FROM chunks WHERE content_hash=?",
            ("x",)))
    assert "idx_chunks_content_hash" in plan


V = [0.1, 0.2, 0.3, 0.4]


def _drive(fid, i, n):
    return {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
            "chunk_total": n, "mime_type": "application/pdf"}


def test_reflow_moves_dup_to_new_canonical_id_or_releases(tmp_path):
    from mcpbrain.reflow import plan
    from mcpbrain.sync.normalise import Chunk
    s = _store(tmp_path)
    other = BODY.replace("Quarterly", "Annual")
    _put(s, "gdrive-F-0", other, _drive("F", 0, 2))
    _put(s, "gdrive-F-1", BODY, _drive("F", 1, 2))
    s.mark_enriched(["gdrive-F-0", "gdrive-F-1"])
    _put(s, "gm-b", BODY, _mail("t2"))
    assert _row(s, "gm-b")[2] == "gdrive-F-1"
    # Reflow F into one chunk: the canonical text no longer exists anywhere.
    new = [Chunk("gdrive-F-0", other + "\n" + BODY, content_hash(other + "\n" + BODY),
                 {"source_type": "gdrive", "file_id": "F", "chunk_index": 0,
                  "chunk_total": 1, "extraction_version": 1}, [other, BODY])]
    s.apply_reflow("F", "drive", plan(s.owner_chunks(["gdrive-F-"]), new), [V])
    assert _row(s, "gm-b") == (0, "", None)


def test_cache_import_changing_canonical_releases_dup(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    s.mark_enriched(["gm-a"])
    _put(s, "gm-b", BODY, _mail("t2"))
    s.import_cached_chunk("gm-a", BODY + " v2", content_hash(BODY + " v2"),
                          _mail("t1"), V, enriched=True)
    assert _row(s, "gm-b") == (0, "", None)


def test_dup_own_change_clears_dup_of(tmp_path):
    s = _store(tmp_path)
    _put(s, "gm-a", BODY, _mail("t1"))
    s.mark_enriched(["gm-a"])
    _put(s, "gm-b", BODY, _mail("t2"))
    _put(s, "gm-b", BODY + " Reply.", _mail("t2"))
    assert _row(s, "gm-b") == (0, "", None)
