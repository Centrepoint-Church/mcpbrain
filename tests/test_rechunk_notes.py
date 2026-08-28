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
