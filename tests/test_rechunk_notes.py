"""bin/rechunk_notes.py: retroactive re-chunk sweep for oversize captured notes.

1,192 pre-Task-7 notes were captured before chunk_text existed and each still
lives as one oversize row where only the first ~2,000 chars are embedded.
plan() finds notes whose body needs more than one chunk_text piece and isn't
already split; apply() rewrites them as suffixed chunks and deletes the old
whole-body row. Round-trip through store.note_chunks() must be lossless.
"""
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_plan_selects_only_oversize_single_chunk_notes(tmp_path):
    from bin import rechunk_notes

    s = _store(tmp_path)
    s.upsert_chunk("note-short", "tiny", "h1", {"source": "note", "title": "a"})
    s.upsert_chunk("note-long", "\n\n".join("z" * 500 for _ in range(20)), "h2",
                   {"source": "note", "title": "b"})
    plan = rechunk_notes.plan(s)
    assert [p["note_id"] for p in plan] == ["note-long"]


def test_plan_skips_notes_already_split(tmp_path):
    from bin import rechunk_notes

    s = _store(tmp_path)
    base = "note-abc"
    for i, piece in enumerate(["first", "second", "third"]):
        s.upsert_chunk(f"{base}-{i}", piece, "h",
                       {"source": "note", "title": "T", "note_id": base,
                        "chunk_index": i, "chunk_total": 3})
    plan = rechunk_notes.plan(s)
    assert plan == []


def test_apply_is_lossless(tmp_path):
    from bin import rechunk_notes

    s = _store(tmp_path)
    body = "\n\n".join(f"para{i} " + "z" * 500 for i in range(20))
    s.upsert_chunk("note-long", body, "h2", {"source": "note", "title": "b"})
    n = rechunk_notes.apply(s, rechunk_notes.plan(s))
    assert n == 1
    rows = s.note_chunks()
    assert len(rows) == 1
    assert rows[0]["text"] == body          # round-trips exactly
    assert rows[0]["doc_id"] == "note-long"


def test_apply_deletes_the_old_whole_body_row(tmp_path):
    from bin import rechunk_notes

    s = _store(tmp_path)
    body = "\n\n".join(f"para{i} " + "z" * 500 for i in range(20))
    s.upsert_chunk("note-long", body, "h2", {"source": "note", "title": "b"})
    rechunk_notes.apply(s, rechunk_notes.plan(s))
    with s._connect() as db:
        row = db.execute("SELECT 1 FROM chunks WHERE doc_id=?", ("note-long",)).fetchone()
        assert row is None
        fts_row = db.execute(
            "SELECT 1 FROM fts_chunks WHERE rowid NOT IN (SELECT rowid FROM chunks)"
        ).fetchone()
        assert fts_row is None


def test_apply_is_a_noop_on_empty(tmp_path):
    from bin import rechunk_notes

    s = _store(tmp_path)
    assert rechunk_notes.apply(s, []) == 0


def test_plan_skips_a_note_whose_split_is_not_lossless(tmp_path, monkeypatch):
    """apply() DELETES the original whole-body row -- the only copy of the note's
    text -- so plan() must exclude any note whose re-chunk cannot be VERIFIED
    lossless, leaving it exactly as it is.

    split_lossless makes this unreachable in practice (all 930 previously-skipped
    live notes now round-trip), so the guard is exercised by forcing a bad split
    rather than by a real input. Keeping it matters: the delete is irreversible.
    """
    from bin import rechunk_notes

    s = _store(tmp_path)
    body = " ".join(f"word{i}" for i in range(700))
    s.upsert_chunk("note-para", body, "h", {"source": "note", "title": "b"})
    monkeypatch.setattr(rechunk_notes, "split_lossless",
                        lambda text, max_chars=1800: ["broken", "pieces"])

    assert rechunk_notes.plan(s) == []
    rows = s.note_chunks()
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "note-para"
    assert rows[0]["text"] == body            # completely untouched


def test_scan_reports_the_skipped_count(tmp_path):
    """main() prints it, so an operator can see a note was deliberately left.

    Both of these now re-chunk losslessly -- the single-paragraph note is
    exactly the shape split_lossless was added for -- so the expected count is
    zero. It was 1 while the sweep split with chunk_text.
    """
    from bin import rechunk_notes

    s = _store(tmp_path)
    s.upsert_chunk("note-para", " ".join(f"word{i}" for i in range(700)), "h",
                   {"source": "note", "title": "b"})
    s.upsert_chunk("note-ok", "\n\n".join(f"para{i} " + "z" * 500 for i in range(20)),
                   "h2", {"source": "note", "title": "c"})
    # Both re-chunk losslessly now; the skip path is exercised by forcing a bad
    # split in test_plan_skips_a_note_whose_split_is_not_lossless above.
    items, skipped = rechunk_notes.scan(s)
    assert sorted(i["note_id"] for i in items) == ["note-ok", "note-para"]
    assert skipped == 0


def test_apply_never_deletes_a_note_whose_pieces_are_lossy(tmp_path):
    """Defense in depth: even handed a hand-built plan item whose pieces do not
    round-trip, apply() must not delete the only copy of the note's text."""
    from bin import rechunk_notes

    s = _store(tmp_path)
    body = " ".join(f"word{i}" for i in range(700))
    s.upsert_chunk("note-para", body, "h", {"source": "note", "title": "b"})
    item = {"note_id": "note-para", "text": body,
            "metadata": {"source": "note", "title": "b"},
            "pieces": ["not", "the", "original"]}
    assert rechunk_notes.apply(s, [item]) == 0
    with s._connect() as db:
        assert db.execute("SELECT 1 FROM chunks WHERE doc_id=?",
                          ("note-para",)).fetchone() is not None
        assert db.execute("SELECT 1 FROM chunks WHERE doc_id=?",
                          ("note-para-0",)).fetchone() is None


def test_sweep_marks_pieces_lossless_so_they_reassemble_exactly(tmp_path):
    """The sweep DELETES the whole-body row, so note_chunks' reassembly becomes
    the note. Pieces from split_lossless carry their own separators and must be
    marked so note_chunks rejoins them with "" — without the marker it would
    inject a blank line at every boundary that was never in the note."""
    from bin import rechunk_notes
    s = _store(tmp_path)
    body = " ".join(f"word{i}" for i in range(700))    # one oversize paragraph
    s.upsert_chunk("note-para", body, "h",
                   {"source": "note", "observation_type": "memory", "title": "T"})

    items = rechunk_notes.plan(s)
    assert [i["note_id"] for i in items] == ["note-para"]
    assert rechunk_notes.apply(s, items) == 1

    rows = s.note_chunks(observation_type="memory")
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "note-para"
    assert rows[0]["metadata"]["split"] == "lossless"
    assert rows[0]["text"] == body                     # byte-exact


def test_sweep_no_longer_skips_single_paragraph_notes(tmp_path):
    """These were the 930 skipped on the live store — the whole remaining hole."""
    from bin import rechunk_notes
    s = _store(tmp_path)
    s.upsert_chunk("note-para", "z" * 9000, "h",
                   {"source": "note", "observation_type": "memory", "title": "T"})
    items, skipped = rechunk_notes.scan(s)
    assert skipped == 0
    assert len(items) == 1


def test_sweep_never_splits_the_fixed_id_identity_seed(tmp_path):
    """memory_tier.seed_core_identity writes note-core-identity-seed at a FIXED
    doc_id and then calls set_chunk_type / set_chunk_tier / set_chunk_salience
    on that exact id. Splitting it into -0/-1 silently breaks all four: the base
    id would have no row. It is 2,836 chars live — a ~1,000-char tail past the
    embed window, which is a far smaller cost than breaking core-tier identity.
    """
    from bin import rechunk_notes
    s = _store(tmp_path)
    body = "\n\n".join(f"para{i} " + "z" * 400 for i in range(12))
    s.upsert_chunk("note-core-identity-seed", body, "h",
                   {"source": "note", "observation_type": "memory", "title": "T"})
    items, skipped = rechunk_notes.scan(s)
    assert items == []
    assert skipped == 0                      # deliberately excluded, not a failure
