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


def _md(fid, i=0, n=1, **extra):
    """A My Drive chunk (no drive_id): the carry-eligible Drive shape."""
    return {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
            "chunk_total": n, "mime_type": "application/pdf", **extra}


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
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-B-0", BODY, _md("B"))
    assert _row(s, "gdrive-B-0") == (1, "duplicate", "gdrive-A-0")
    assert "gdrive-B-0" not in _unenriched_ids(s)
    assert s.canonical_for("gdrive-B-0") == "gdrive-A-0"
    assert s.canonical_for("gdrive-A-0") == "gdrive-A-0"
    assert s.canonical_for("missing") == "missing"


def test_identical_note_is_carried(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    nid = "note-" + "a" * 32
    _put(s, nid, BODY, {"source": "note", "note_id": nid})
    assert _row(s, nid) == (1, "duplicate", "gdrive-A-0")


def test_gmail_body_and_attachment_never_carried(tmp_path):
    """A carried message never enters a unit: its sender/recipient links and
    thread actions would be lost. Both copies are extracted."""
    s = _store(tmp_path)
    _put(s, "gmail-m1-body-0", BODY, _mail("t1"))
    _put(s, "gmail-m1-att-0-0", BODY + " att", _mail("t1", content_type="email_attachment"))
    s.mark_enriched(["gmail-m1-body-0", "gmail-m1-att-0-0"])
    _put(s, "gmail-m2-body-0", BODY, _mail("t2"))
    _put(s, "gmail-m2-att-0-0", BODY + " att", _mail("t2", content_type="email_attachment"))
    assert _row(s, "gmail-m2-body-0") == (0, "", None)
    assert _row(s, "gmail-m2-att-0-0") == (0, "", None)
    assert {"gmail-m2-body-0", "gmail-m2-att-0-0"} <= _unenriched_ids(s)
    # ...while a My Drive copy of the same text IS carried (from the mail).
    _put(s, "gdrive-B-0", BODY, _md("B"))
    assert _row(s, "gdrive-B-0") == (1, "duplicate", "gmail-m1-body-0")


def test_shared_drive_chunk_never_carried_and_payload_publishes(tmp_path):
    """A carried chunk is in no unit, so drain's covers would never list it
    and enrich_payload_covers_file would withhold the file's payload forever."""
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    other = BODY.replace("Quarterly", "Annual")
    _put(s, "gdrive-S-0", BODY, _md("S", 0, 2, drive_id="0AShared"))
    _put(s, "gdrive-S-1", other, _md("S", 1, 2, drive_id="0AShared"))
    assert _row(s, "gdrive-S-0") == (0, "", None)
    # drain extracts the file as one unit; its covers are the unit's chunks.
    s.mark_enriched(["gdrive-S-0", "gdrive-S-1"])
    s.set_enrich_payload("S", "{}", 1, covers=["gdrive-S-0", "gdrive-S-1"])
    assert s.enrich_payload_covers_file("S") is True


def test_short_chunks_not_carried(tmp_path):
    s = _store(tmp_path)
    short = "Thanks, see you Sunday."
    _put(s, "gdrive-A-0", short, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-B-0", short, _md("B"))
    assert _row(s, "gdrive-B-0") == (0, "", None)


def test_changed_canonical_does_not_orphan_dup(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-B-0", BODY, _md("B"))
    assert _row(s, "gdrive-B-0")[2] == "gdrive-A-0"
    _put(s, "gdrive-A-0", BODY + " Edited.", _md("A"))
    assert _row(s, "gdrive-B-0") == (0, "", None)
    assert "gdrive-B-0" in _unenriched_ids(s)


def test_deleted_canonical_releases_dup(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-B-0", BODY, _md("B"))
    s.delete_chunks(["gdrive-A-0"])
    assert _row(s, "gdrive-B-0") == (0, "", None)


def test_unenriched_canonical_not_carried(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    _put(s, "gdrive-B-0", BODY, _md("B"))
    assert _row(s, "gdrive-B-0") == (0, "", None)


def test_cold_canonical_not_carried(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    s.set_enrich_state(["gdrive-A-0"], "cold")
    _put(s, "gdrive-B-0", BODY, _md("B"))
    assert _row(s, "gdrive-B-0")[2] is None


def test_derived_canonical_not_carried(tmp_path):
    """A primary document is never made a duplicate of a draft/derived copy."""
    s = _store(tmp_path)
    _put(s, "gm-draft", BODY, _mail("t1", labels="DRAFT"))
    _put(s, "gdrive-G-0", BODY, _md("G", app_generated="1"))
    _put(s, "note-consolidated-x", BODY, {"source": "note"})
    s.mark_enriched(["gm-draft", "gdrive-G-0", "note-consolidated-x"])
    _put(s, "gdrive-B-0", BODY, _md("B"))
    assert _row(s, "gdrive-B-0") == (0, "", None)


def test_derived_new_chunk_not_marked_duplicate(tmp_path):
    """A generated (brief) copy of a primary document keeps its own class."""
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-G-0", BODY, _md("G", app_generated="1"))
    assert _row(s, "gdrive-G-0")[2] is None


def test_oldest_enriched_evidence_is_canonical(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    _put(s, "gdrive-B-0", BODY, _md("B"))
    s.mark_enriched(["gdrive-A-0", "gdrive-B-0"])
    _put(s, "gdrive-C-0", BODY, _md("C"))
    assert _row(s, "gdrive-C-0")[2] == "gdrive-A-0"


def test_dup_never_chains(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-B-0", BODY, _md("B"))
    s.delete_chunks(["gdrive-A-0"])        # B released, enriched=0
    _put(s, "gdrive-C-0", BODY, _md("C"))
    assert _row(s, "gdrive-C-0")[2] is None  # B is not enriched: nothing to carry


def test_version_reflow_does_not_reset_dup(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-B-0", BODY, _md("B"))
    s.reflow_outdated_chunks(version=10**6, cap=100)
    assert _row(s, "gdrive-B-0") == (1, "duplicate", "gdrive-A-0")


def test_content_hash_lookup_is_indexed(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        plan = " ".join(r[-1] for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT doc_id FROM chunks WHERE content_hash=?",
            ("x",)))
    assert "idx_chunks_content_hash" in plan


V = [0.1, 0.2, 0.3, 0.4]


def _reflow(s, fid, texts):
    from mcpbrain.reflow import plan
    from mcpbrain.sync.normalise import Chunk
    new = [Chunk(f"gdrive-{fid}-{i}", t, content_hash(t),
                 {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
                  "chunk_total": len(texts), "extraction_version": 1}, [t])
           for i, t in enumerate(texts)]
    s.apply_reflow(fid, "drive", plan(s.owner_chunks([f"gdrive-{fid}-"]), new),
                   [V] * len(new))


def test_reflow_of_canonical_releases_dup_when_text_gone(tmp_path):
    s = _store(tmp_path)
    other = BODY.replace("Quarterly", "Annual")
    _put(s, "gdrive-F-0", other, _md("F", 0, 2))
    _put(s, "gdrive-F-1", BODY, _md("F", 1, 2))
    s.mark_enriched(["gdrive-F-0", "gdrive-F-1"])
    _put(s, "gdrive-B-0", BODY, _md("B"))
    assert _row(s, "gdrive-B-0")[2] == "gdrive-F-1"
    # Reflow F into one chunk: the canonical text no longer exists anywhere.
    _reflow(s, "F", [other + "\n" + BODY])
    assert _row(s, "gdrive-B-0") == (0, "", None)


def test_reflow_of_dup_keeps_dup_of_when_text_moves(tmp_path):
    """The duplicate's OWN owner re-chunks: identical text at a new id keeps
    dup_of; a chunk whose text changed is no longer 'duplicate'."""
    s = _store(tmp_path)
    other = BODY.replace("Quarterly", "Annual")
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-D-0", other, _md("D", 0, 2))
    _put(s, "gdrive-D-1", BODY, _md("D", 1, 2))
    s.mark_enriched(["gdrive-D-0"])
    assert _row(s, "gdrive-D-1") == (1, "duplicate", "gdrive-A-0")
    intro = "Preface. " * 30
    _reflow(s, "D", [intro, other, BODY])
    assert _row(s, "gdrive-D-2")[2] == "gdrive-A-0"
    assert _row(s, "gdrive-D-2")[1] == "duplicate"
    for d in ("gdrive-D-0", "gdrive-D-1"):
        r = _row(s, d)
        assert r[2] is None and r[1] != "duplicate", (d, r)


def test_cache_import_changing_canonical_releases_dup(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-B-0", BODY, _md("B"))
    s.import_cached_chunk("gdrive-A-0", BODY + " v2", content_hash(BODY + " v2"),
                          _md("A"), V, enriched=True)
    assert _row(s, "gdrive-B-0") == (0, "", None)


def test_dup_own_change_clears_dup_of(tmp_path):
    s = _store(tmp_path)
    _put(s, "gdrive-A-0", BODY, _md("A"))
    s.mark_enriched(["gdrive-A-0"])
    _put(s, "gdrive-B-0", BODY, _md("B"))
    _put(s, "gdrive-B-0", BODY + " Reply.", _md("B"))
    assert _row(s, "gdrive-B-0") == (0, "", None)
