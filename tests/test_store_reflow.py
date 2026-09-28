import pytest

from mcpbrain.reflow import plan
from mcpbrain.store import _REFLOW_REF_COLUMNS, ReflowOrphanError, Store
from mcpbrain.sync.normalise import Chunk


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


V = [0.1, 0.2, 0.3, 0.4]


def _seed(s, fid="F", texts=("alpha beta gamma", "delta epsilon"), state=None):
    for i, t in enumerate(texts):
        meta = {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
                "chunk_total": len(texts), "mime_type": "application/pdf"}
        s.upsert_chunk(f"gdrive-{fid}-{i}", t, f"h{i}", meta)
        with s._connect(write=True) as db:
            db.execute("UPDATE chunks SET enriched=1, enriched_version=3, enrich_state=? "
                       "WHERE doc_id=?", (state, f"gdrive-{fid}-{i}"))


def _new(fid, texts):
    return [Chunk(f"gdrive-{fid}-{i}", t, f"n{i}",
                  {"source_type": "gdrive", "file_id": fid, "chunk_index": i,
                   "chunk_total": len(texts), "extraction_version": 1}, [t])
            for i, t in enumerate(texts)]


def _relation(s, doc_id, rel="works_at"):
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type) VALUES('e1','Dana Okafor','person')"
                   " ON CONFLICT DO NOTHING")
        db.execute("INSERT INTO entities(id, name, type) VALUES('e2','Northgate Trust','org')"
                   " ON CONFLICT DO NOTHING")
        db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, source_doc_id)"
                   " VALUES('e1',?,'e2',?)", (rel, doc_id))


def _all_refs(s, doc_id):
    """One row in EVERY _REFLOW_REF_COLUMNS target, pointing at doc_id."""
    _relation(s, doc_id)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO entity_observations(entity_id, attribute, value, source)"
                   " VALUES('e1','role','Treasurer',?)", (doc_id,))
        db.execute("INSERT INTO actions(text, source_doc_id, waiting_on_cleared_by_doc_id)"
                   " VALUES('Send the budget',?,?)", (doc_id, doc_id))
        db.execute("INSERT INTO graph_actions_legacy(text, source_doc_id) VALUES('x',?)",
                   (doc_id,))
        db.execute("INSERT INTO graph_decisions_legacy(text, source_doc_id) VALUES('y',?)",
                   (doc_id,))
        db.execute("INSERT INTO recall_feedback(doc_id, event_type) VALUES(?,'exposure')",
                   (doc_id,))


def _ref_values(s):
    out = {}
    with s._connect() as db:
        for table, col in _REFLOW_REF_COLUMNS:
            out[(table, col)] = sorted(r[0] for r in db.execute(f"SELECT {col} FROM {table}")
                                       if r[0])
    return out


def test_apply_reflow_merges_to_one_chunk_and_remaps_relation(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    _relation(s, "gdrive-F-1")
    old = s.owner_chunks(["gdrive-F-"])
    p = plan(old, _new("F", ["alpha beta gamma\ndelta epsilon"]))
    out = s.apply_reflow("F", "drive", p, [V])
    assert out["deleted"] == 1 and out["carried"] == 1
    with s._connect() as db:
        rel = db.execute("SELECT source_doc_id FROM entity_relations").fetchone()[0]
        row = db.execute("SELECT enriched, embedded, text FROM chunks WHERE doc_id='gdrive-F-0'").fetchone()
        n = db.execute("SELECT count(*) FROM chunks").fetchone()[0]
        nvec = db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0]
        logged = db.execute("SELECT old_doc_id, new_doc_id FROM reflow_map ORDER BY old_doc_id").fetchall()
    assert rel == "gdrive-F-0"
    assert tuple(row) == (1, 1, "alpha beta gamma\ndelta epsilon")
    assert n == 1 and nvec == 1
    assert [tuple(r) for r in logged] == [("gdrive-F-0", "gdrive-F-0"), ("gdrive-F-1", "gdrive-F-0")]
    assert s.latest_reflow_target("gdrive-F-1") == "gdrive-F-0"
    assert s.read_doc("gdrive-F-1")["doc_id"] == "gdrive-F-0"
    assert [d for d, _ in s.fts_search("epsilon", 5)] == ["gdrive-F-0"]
    st = s.reflow_stats()
    assert {k: st[k] for k in ("owners_done", "chunks_carried", "chunks_reenrich",
                               "queued", "by_outcome")} == {
        "owners_done": 1, "chunks_carried": 1, "chunks_reenrich": 0, "queued": 0,
        "by_outcome": {"carried": 1}}


def test_every_reference_table_is_remapped(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    _all_refs(s, "gdrive-F-1")
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["alpha beta gamma\ndelta epsilon"]))
    out = s.apply_reflow("F", "drive", p, [V])
    assert out["remapped"] == len(_REFLOW_REF_COLUMNS)
    assert all(v == ["gdrive-F-0"] for v in _ref_values(s).values()), _ref_values(s)


def test_apply_reflow_simultaneous_swap(tmp_path):
    """Remap i->j and j->i in one statement must not chain."""
    s = _store(tmp_path)
    _seed(s, texts=("first part", "second part"))
    _relation(s, "gdrive-F-0", "works_at")
    _relation(s, "gdrive-F-1", "member_of")
    old = s.owner_chunks(["gdrive-F-"])
    p = plan(old, _new("F", ["first part", "second part"]))
    p.remap = {"gdrive-F-0": "gdrive-F-1", "gdrive-F-1": "gdrive-F-0"}
    s.apply_reflow("F", "drive", p, [V, V])
    with s._connect() as db:
        got = dict(db.execute("SELECT relation, source_doc_id FROM entity_relations").fetchall())
    assert got == {"works_at": "gdrive-F-1", "member_of": "gdrive-F-0"}


def test_apply_reflow_preserves_cold(tmp_path):
    s = _store(tmp_path)
    _seed(s, state="cold")
    old = s.owner_chunks(["gdrive-F-"])
    s.apply_reflow("F", "drive", plan(old, _new("F", ["alpha beta gamma", "delta epsilon"])), [V, V])
    with s._connect() as db:
        states = {r[0] for r in db.execute("SELECT enrich_state FROM chunks")}
    assert states == {"cold"}


def test_carried_state_columns(tmp_path):
    s = _store(tmp_path)
    _seed(s, texts=("alpha beta gamma",))
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET salience=0.7, memory_tier='core', memory_type='semantic',"
                   " enrich_attempts=2")
    old = s.owner_chunks(["gdrive-F-"])
    s.apply_reflow("F", "drive", plan(old, _new("F", ["alpha beta\ngamma"])), [V])
    with s._connect() as db:
        r = db.execute("SELECT enriched, enriched_version, salience, memory_tier, memory_type,"
                       " enrich_attempts, metadata FROM chunks").fetchone()
    assert tuple(r)[:6] == (1, 3, 0.7, "core", "semantic", 0)
    assert '"extraction_version": 1' in r[6]


def _snapshot(s):
    with s._connect() as db:
        return {t: [tuple(r) for r in db.execute(f"SELECT * FROM {t} ORDER BY rowid")]
                for t in ("chunks", "entity_relations", "reflow_map", "reflow_owners",
                          "chunk_quality", "recall_feedback", "actions")}


def test_orphan_guard_rolls_back(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    _relation(s, "gdrive-F-1")
    before = _snapshot(s)
    old = s.owner_chunks(["gdrive-F-"])
    p = plan(old, _new("F", ["alpha beta gamma\ndelta epsilon"]))
    p.remap["gdrive-F-1"] = "gdrive-F-9"          # a target that will not exist
    with pytest.raises(ReflowOrphanError):
        s.apply_reflow("F", "drive", p, [V])
    with s._connect() as db:
        assert db.execute("SELECT count(*) FROM chunks").fetchone()[0] == 2
        assert db.execute("SELECT source_doc_id FROM entity_relations").fetchone()[0] == "gdrive-F-1"
        assert db.execute("SELECT count(*) FROM reflow_map").fetchone()[0] == 0
    assert _snapshot(s) == before


def test_orphan_guard_catches_a_reference_the_remap_missed(tmp_path):
    """The dangling-reference count itself, independent of the target check.
    A trigger writes a reference to a deleted id AFTER the remap ran (standing
    in for any reference the remap did not reach); the guard must see it and
    the whole transaction -- the trigger's own write included -- roll back."""
    s = _store(tmp_path)
    _seed(s)
    _all_refs(s, "gdrive-F-1")
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunk_quality(doc_id, exposures, uses) VALUES('gdrive-F-1', 2, 1)")
        db.execute("CREATE TRIGGER late_ref AFTER INSERT ON reflow_map BEGIN "
                   "INSERT INTO recall_feedback(doc_id, event_type) "
                   "VALUES('gdrive-F-1', 'late'); END")
    before = _snapshot(s)
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["alpha beta gamma\ndelta epsilon"]))
    with pytest.raises(ReflowOrphanError, match=r"dangling reference\(s\), 0 remap target"):
        s.apply_reflow("F", "drive", p, [V])
    assert _snapshot(s) == before
    with s._connect() as db:
        assert db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 0   # never embedded
        assert "reflow_tmp" not in {r[0] for r in db.execute(
            "SELECT name FROM sqlite_temp_master")}


def test_uncovered_chunk_is_hot_and_unenriched(tmp_path):
    s = _store(tmp_path)
    _seed(s, texts=("alpha beta gamma",))
    old = s.owner_chunks(["gdrive-F-"])
    out = s.apply_reflow("F", "drive", plan(old, _new("F", ["alpha beta gamma", "Notes: new"])),
                         [V, V])
    with s._connect() as db:
        r = db.execute("SELECT enriched, enrich_state FROM chunks WHERE doc_id='gdrive-F-1'").fetchone()
    assert tuple(r) == (0, None)
    assert out["carried"] == 1 and out["reenrich"] == 1


def test_chunk_quality_is_merged(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunk_quality(doc_id, quality, exposures, uses, memory_strength,"
                   " last_accessed) VALUES('gdrive-F-0', 0.4, 3.5, 1.0, 4.0, '2026-09-01')")
        db.execute("INSERT INTO chunk_quality(doc_id, quality, exposures, uses, memory_strength,"
                   " last_accessed) VALUES('gdrive-F-1', 0.9, 1.25, 0.5, 7.0, '2026-08-01')")
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["alpha beta gamma\ndelta epsilon"]))
    s.apply_reflow("F", "drive", p, [V])
    with s._connect() as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM chunk_quality")]
    assert len(rows) == 1
    r = rows[0]
    assert r["doc_id"] == "gdrive-F-0"
    assert (r["exposures"], r["uses"]) == (4.75, 1.5)
    assert (r["quality"], r["memory_strength"], r["last_accessed"]) == (0.9, 7.0, "2026-09-01")


def test_chunk_quality_swap_does_not_double_count(tmp_path):
    s = _store(tmp_path)
    _seed(s, texts=("first part", "second part"))
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunk_quality(doc_id, exposures, uses) VALUES('gdrive-F-0', 1, 0)")
        db.execute("INSERT INTO chunk_quality(doc_id, exposures, uses) VALUES('gdrive-F-1', 5, 2)")
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["first part", "second part"]))
    p.remap = {"gdrive-F-0": "gdrive-F-1", "gdrive-F-1": "gdrive-F-0"}
    s.apply_reflow("F", "drive", p, [V, V])
    with s._connect() as db:
        got = {r[0]: (r[1], r[2]) for r in db.execute(
            "SELECT doc_id, exposures, uses FROM chunk_quality")}
    assert got == {"gdrive-F-1": (1.0, 0.0), "gdrive-F-0": (5.0, 2.0)}


def test_owner_chunks_is_case_sensitive_and_positional(tmp_path):
    s = _store(tmp_path)
    for d in ("gdrive-abc-0", "gdrive-abc-1", "gdrive-ABC-0", "gdrive-abc-def-0",
              "cal-e1", "cal-e1-1", "cal-e1_20260901T090000Z", "cal-e10",
              "gmail-m1-body-0", "gmail-m1-att-2-0", "gmail-m10-body-0",
              "anarlog-s1-note-0", "anarlog-s1-transcript-3"):
        s.upsert_chunk(d, "t " + d, d, {"source_type": "x"})
    ids = lambda pfx: [r["doc_id"] for r in s.owner_chunks(pfx)]  # noqa: E731
    assert ids(["gdrive-abc-"]) == ["gdrive-abc-0", "gdrive-abc-1"]
    assert ids(["gdrive-abc"]) == ["gdrive-abc-0", "gdrive-abc-1"]
    assert ids(["cal-e1-"]) == ["cal-e1-1"]
    assert ids(["cal-e1"]) == ["cal-e1", "cal-e1-1"]
    assert ids(["gmail-m1-"]) == ["gmail-m1-body-0", "gmail-m1-att-2-0"]
    assert ids(["gmail-m1-att-"]) == ["gmail-m1-att-2-0"]
    assert ids(["anarlog-s1-"]) == ["anarlog-s1-note-0", "anarlog-s1-transcript-3"]
    assert ids(["gdrive-abc-", "gdrive-abc-"]) == ["gdrive-abc-0", "gdrive-abc-1"]
    row = s.owner_chunks(["gdrive-abc-"])[0]
    assert set(row) == {"doc_id", "text", "metadata", "enriched", "enriched_version",
                        "enrich_state", "salience", "memory_tier", "memory_type"}
    assert row["metadata"] == {"source_type": "x"}


def test_stale_ids_resolve_through_a_chain(tmp_path):
    s = _store(tmp_path)
    _seed(s, texts=("a1 a2", "b1 b2", "c1 c2"))
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["a1 a2\nb1 b2", "c1 c2"]))
    s.apply_reflow("F", "drive", p, [V, V])          # F-2 -> F-1
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["a1 a2\nb1 b2\nc1 c2"]))
    s.apply_reflow("F", "drive", p, [V])             # F-1 -> F-0
    assert s.latest_reflow_target("gdrive-F-2") == "gdrive-F-1"
    assert s.resolve_reflowed_ids(["gdrive-F-2", "gdrive-F-0", "gdrive-F-1", "nope"]) \
        == ["gdrive-F-0"]
    assert s.read_doc("gdrive-F-2")["doc_id"] == "gdrive-F-0"
    assert s.latest_reflow_target("never-reflowed") is None


def test_drain_part_ids_fall_back_to_reflow_target(tmp_path):
    from mcpbrain import drain as drain_mod
    s = _store(tmp_path)
    _seed(s)
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["alpha beta gamma\ndelta epsilon"]))
    s.apply_reflow("F", "drive", p, [V])
    ext = {"thread_id": "F", "part_doc_ids": ["gdrive-F-0", "gdrive-F-1"]}
    assert drain_mod._resolve_doc_ids(s, ext, {}) == ["gdrive-F-0"]


def test_org_provenance_falls_back_to_reflow_target(tmp_path):
    from mcpbrain.org_contrib import _chunk_provenance
    s = _store(tmp_path)
    _seed(s)
    assert _chunk_provenance(s, "gdrive-F-1") == (True, "drive")   # source_type gdrive
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["alpha beta gamma\ndelta epsilon"]))
    s.apply_reflow("F", "drive", p, [V])
    assert _chunk_provenance(s, "gdrive-F-1") == (True, "drive")
    assert _chunk_provenance(s, "gdrive-G-1") == (False, "unknown")


def test_enqueue_items_does_not_touch_cursor(tmp_path):
    s = _store(tmp_path)
    item = {"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}
    n = s.enqueue_items([item], source="reflow:drive")
    assert n == 1
    assert s.get_cursor("reflow:drive") is None
    assert s.reflow_stats()["queued"] == 1
    assert s.enqueue_items([item], source="reflow:drive") == 0      # already queued
    assert s.reflow_stats()["queued"] == 1


def test_apply_reflow_rejects_malformed_plans(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["alpha beta gamma\ndelta epsilon"]))
    with pytest.raises(ValueError):
        s.apply_reflow("F", "drive", p, [])
    p.deletes = ["gdrive-OTHER-0"]
    with pytest.raises(ValueError):
        s.apply_reflow("F", "drive", p, [V])


def test_apply_reflow_refuses_to_delete_a_whole_lineage(tmp_path):
    """An attachment lineage with no new counterpart (its re-extraction came
    back empty) must never be deleted by a reflow: refuse before any write."""
    s = _store(tmp_path)
    for d, t in (("gmail-M-body-0", "body text here"), ("gmail-M-att-0-0", "attachment words")):
        s.upsert_chunk(d, t, "h-" + d, {"source_type": "gmail", "message_id": "M",
                                        "chunk_index": 0})
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET enriched=1, enriched_version=3")
    _all_refs(s, "gmail-M-att-0-0")
    before = _snapshot(s)
    raw_before = (tmp_path / "a.sqlite3").read_bytes()
    new = [Chunk("gmail-M-body-0", "body text here", "n0",
                 {"source_type": "gmail", "message_id": "M", "chunk_index": 0},
                 ["body text here"])]
    p = plan(s.owner_chunks(["gmail-M-"]), new)
    assert p.reasons["gmail-M-att-0-0"] == "lineage_gone"
    assert "gmail-M-att-0" in p.unequal
    with pytest.raises(ValueError, match="lineage"):
        s.apply_reflow("M", "gmail", p, [V])
    assert _snapshot(s) == before
    assert (tmp_path / "a.sqlite3").read_bytes() == raw_before


def test_orphan_guard_is_not_blinded_by_a_null_doc_id_chunk(tmp_path):
    """Final review I7: `col NOT IN (SELECT doc_id FROM chunks)` is never true
    once any chunks.doc_id is NULL, so the guard silently counted 0."""
    s = _store(tmp_path)
    _seed(s)
    _all_refs(s, "gdrive-F-1")
    with s._connect(write=True) as db:
        db.execute("INSERT INTO chunks(doc_id, text, content_hash, metadata) "
                   "VALUES(NULL, 'stray', 'x', '{}')")
        db.execute("CREATE TRIGGER late_ref AFTER INSERT ON reflow_map BEGIN "
                   "INSERT INTO recall_feedback(doc_id, event_type) "
                   "VALUES('gdrive-F-1', 'late'); END")
    p = plan(s.owner_chunks(["gdrive-F-"]), _new("F", ["alpha beta gamma\ndelta epsilon"]))
    with pytest.raises(ReflowOrphanError, match="dangling"):
        s.apply_reflow("F", "drive", p, [V])


def test_uncovered_row_gets_fresh_defaults_not_the_positional_old_chunks(tmp_path):
    """Final-review investigation: an uncovered new chunk written over a reused
    positional id kept that id's OLD salience/memory_tier/memory_type through
    COALESCE -- state scored for different text. It gets the column defaults."""
    s = _store(tmp_path)
    _seed(s, texts=("alpha beta gamma", "delta epsilon"))
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET salience=0.9, memory_tier='core', memory_type='semantic'")
    old = s.owner_chunks(["gdrive-F-"])
    new = _new("F", ["alpha beta gamma\ndelta epsilon", "Notes: entirely new remark"])
    s.apply_reflow("F", "drive", plan(old, new), [V, V])
    with s._connect() as db:
        rows = {r[0]: tuple(r[1:]) for r in db.execute(
            "SELECT doc_id, salience, memory_tier, memory_type, enriched FROM chunks")}
    assert rows["gdrive-F-0"] == (0.9, "core", "semantic", 1)       # covered: carried
    assert rows["gdrive-F-1"] == (0.0, "", "episodic", 0)           # uncovered: fresh


# -- hardening H4: the ordinary path never leaves a stale payload looking whole -

def _covers(s, fid="F"):
    import json
    with s._connect() as db:
        r = db.execute("SELECT covers FROM enrich_payloads WHERE file_id=?", (fid,)).fetchone()
    return None if r is None or r[0] is None else json.loads(r[0])


def _meta(i, fid="F"):
    return {"source_type": "gdrive", "file_id": fid, "chunk_index": i, "chunk_total": 2,
            "mime_type": "application/pdf"}


def test_content_change_drops_the_chunk_from_an_explicit_covers(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    s.set_enrich_payload("F", "{}", 3, covers=["gdrive-F-0", "gdrive-F-1"])
    assert s.enrich_payload_covers_file("F")
    s.upsert_chunk("gdrive-F-1", "changed text", "h1-new", _meta(1))
    assert _covers(s) == ["gdrive-F-0"]
    # A mark_enriched that bypasses drain (give-up, noise filter, trivial
    # short-circuit) must not make the stale payload look whole again.
    s.mark_enriched(["gdrive-F-1"])
    assert not s.enrich_payload_covers_file("F")


def test_content_change_materialises_a_whole_file_null_covers(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    s.set_enrich_payload("F", "{}", 3)            # NULL covers = whole file
    s.upsert_chunk("gdrive-F-1", "changed text", "h1-new", _meta(1))
    assert _covers(s) == ["gdrive-F-0"]
    s.mark_enriched(["gdrive-F-1"])
    assert not s.enrich_payload_covers_file("F")


def test_a_new_chunk_is_never_inside_a_whole_file_null_covers(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    s.set_enrich_payload("F", "{}", 3)
    s.upsert_chunk("gdrive-F-2", "a third chunk", "h2", _meta(2))
    assert _covers(s) == ["gdrive-F-0", "gdrive-F-1"]
    assert not s.enrich_payload_covers_file("F")


def test_unchanged_upsert_leaves_covers_alone(tmp_path):
    s = _store(tmp_path)
    _seed(s)
    s.set_enrich_payload("F", "{}", 3)
    assert s.upsert_chunk("gdrive-F-1", "delta epsilon", "h1", _meta(1)) is False
    assert _covers(s) is None and s.enrich_payload_covers_file("F")


def _legacy(s, rows):
    with s._connect(write=True) as db:
        db.execute("CREATE TABLE enrich_payloads_legacy(doc_id TEXT PRIMARY KEY, "
                   "payload TEXT NOT NULL, logic_version INTEGER DEFAULT 0, "
                   "at TEXT DEFAULT CURRENT_TIMESTAMP)")
        db.executemany("INSERT INTO enrich_payloads_legacy(doc_id,payload,logic_version) "
                       "VALUES(?,?,?)", [(d, "{}", 3) for d in rows])


def test_migrated_legacy_payload_covers_nothing_unless_provably_whole(tmp_path):
    s = _store(tmp_path)
    _seed(s, "F")
    _seed(s, "G")
    _seed(s, "H")
    s.upsert_chunk("gdrive-H-1", "changed since the payload", "h1-new", _meta(1, "H"))
    _legacy(s, ["gdrive-F-0",                      # F: only one of two chunks
                "gdrive-G-0", "gdrive-G-1",        # G: every hot chunk, all enriched
                "gdrive-H-0", "gdrive-H-1"])       # H: chunk 1 changed (enriched=0)
    while not s.migrate_enrich_payloads_batch()["done"]:
        pass
    assert _covers(s, "F") == [] and not s.enrich_payload_covers_file("F")
    assert _covers(s, "G") == ["gdrive-G-0", "gdrive-G-1"]
    assert s.enrich_payload_covers_file("G")
    assert _covers(s, "H") == [] and not s.enrich_payload_covers_file("H")


@pytest.mark.parametrize("bad", ["garbage", "null", "5", "{}"])
def test_malformed_covers_never_fails_the_upsert(tmp_path, bad):
    s = _store(tmp_path)
    _seed(s)
    s.set_enrich_payload("F", "{}", 3, covers=["gdrive-F-0"])
    with s._connect(write=True) as db:
        db.execute("UPDATE enrich_payloads SET covers=? WHERE file_id='F'", (bad,))
    assert s.upsert_chunk("gdrive-F-1", "changed text", "h1-new", _meta(1)) is True
    assert s.get_chunk("gdrive-F-1")["text"] == "changed text"
    with s._connect() as db:
        assert db.execute("SELECT covers FROM enrich_payloads WHERE file_id='F'"
                          ).fetchone()[0] == "[]"
    assert not s.enrich_payload_covers_file("F")
