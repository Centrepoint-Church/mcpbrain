"""Task 9: removal paths take actions/links with them (batched, scoped, reversible)."""
from mcpbrain import ingest_cache
from mcpbrain.store import Store
from mcpbrain.sync.drive import handle_drive_item, handle_shared_drive_item
from tests.helpers.org_fleet import LocalDirFleetStorage
from tests.test_drive_sync import FakeDriveService
from tests.test_drive_shared import PIN


def _s(tmp_path):
    s = Store(tmp_path / "t.sqlite3", dim=4)
    s.init()
    return s


def _seed(s, src, *, thread=None, ent="dana-okafor", count=1, source="email"):
    s.upsert_entity(ent, "Dana Okafor", "person", "", "2026-01-01")
    s.link_email_entity(src, ent, role="sender")
    with s._connect(write=True) as db:
        db.execute("UPDATE entities SET email_count=? WHERE id=?", (count, ent))
    return s.add_unified_action(text="t " + src, source=source, source_doc_id=src,
                                thread_id=thread or src)


def _act(s, i):
    with s._connect() as db:
        return dict(db.execute("SELECT * FROM actions WHERE id=?", (i,)).fetchone())


def _ec(s, ent="dana-okafor"):
    with s._connect() as db:
        return db.execute("SELECT email_count FROM entities WHERE id=?", (ent,)).fetchone()[0]


def test_more_than_sqlite_variable_limit(tmp_path):
    s = _s(tmp_path)
    ids = [f"gdrive-F-{i}" for i in range(34000)]
    a = _seed(s, "F")
    out = s.invalidate_sources_for_docs(ids, source_ids=["F", *ids], reason="source_gone")
    assert out["actions"] == 1 and _act(s, a)["status"] == "auto_archived"


def test_stale_chunk_id_links_and_actions_matched(tmp_path):
    s = _s(tmp_path)
    a = _seed(s, "gdrive-FID-7", ent="dana-okafor")            # stale, reflow-replaced id
    other = _seed(s, "gdrive-FID-1-3", ent="priya-anand")      # DIFFERENT file 'FID-1'
    s.invalidate_sources_for_docs([], file_ids=["FID"], reason="source_gone")
    assert _act(s, a)["status"] == "auto_archived"
    assert _act(s, other)["status"] == "open"
    with s._connect() as db:
        d = dict(db.execute("SELECT message_id, derived FROM email_entities").fetchall())
    assert d["gdrive-FID-7"] == 1 and d["gdrive-FID-1-3"] == 0


def test_double_removal_single_decrement_and_relink_revives(tmp_path):
    s = _s(tmp_path)
    _seed(s, "F", count=3)
    for _ in range(2):
        s.invalidate_sources_for_docs([], source_ids=["F"], reason="source_gone")
    assert _ec(s) == 2
    assert s.link_email_entity("F", "dana-okafor", role="sender") is True   # source came back
    with s._connect() as db:
        r = db.execute("SELECT derived, derived_reason FROM email_entities").fetchone()
    assert tuple(r) == (0, None)


def test_derived_class_link_not_revived(tmp_path):
    s = _s(tmp_path)
    s.upsert_entity("e1", "E", "person", "", "2026-01-01")
    s.link_email_entity("m", "e1", derived=True)
    assert s.link_email_entity("m", "e1") is False


def test_shared_drive_remove_scoped_to_event_drive(tmp_path):
    s = _s(tmp_path)
    fs = LocalDirFleetStorage(tmp_path / "drv")
    s.upsert_chunk("gdrive-FID-0", "x", "h", {"file_id": "FID", "drive_id": "D2"})
    a = _seed(s, "FID")
    item = {"ref_id": "FID", "version": "", "event": "remove", "modified_at": "2026-05-01T10:00:00Z"}
    handle_shared_drive_item(FakeDriveService(), s, item, fleet_storage=fs, pin=PIN, drive_id="D1")
    assert s.get_chunk("gdrive-FID-0") is not None
    assert _act(s, a)["status"] == "open"
    handle_shared_drive_item(FakeDriveService(), s, item, fleet_storage=fs, pin=PIN, drive_id="D2")
    assert s.get_chunk("gdrive-FID-0") is None
    assert _act(s, a)["status"] == "auto_archived"
    assert _ec(s) == 0


def test_my_drive_remove_archives(tmp_path):
    s = _s(tmp_path)
    s.upsert_chunk("gdrive-F1-0", "x", "h", {"file_id": "F1"})
    a = _seed(s, "F1")
    handle_drive_item(FakeDriveService(pages=[]), s, {"ref_id": "F1", "version": "", "event": "remove"})
    assert _act(s, a)["status"] == "auto_archived" and _ec(s) == 0


def test_purge_drive_path_and_reasons(tmp_path):
    s = _s(tmp_path)
    s.upsert_chunk("gdrive-F1-0", "x", "h", {"file_id": "F1", "drive_id": "D1"})
    a = _seed(s, "gdrive-F1-4")                   # stale id, current chunk is -0
    ingest_cache.purge_drive(s, "D1")
    r = _act(s, a)
    assert r["status"] == "auto_archived" and r["resolved_by"] == "drive_revoked"
    assert s.doc_ids_for_drive("D1") == []


def test_purge_drive_infra_reason(tmp_path):
    s = _s(tmp_path)
    s.upsert_chunk("gdrive-F1-0", "x", "h", {"file_id": "F1", "drive_id": "I"})
    a = _seed(s, "F1")
    ingest_cache.purge_drive(s, "I", reason="infra_purged")
    assert _act(s, a)["resolved_by"] == "infra_purged"


def test_stale_probes_are_index_backed(tmp_path):
    s = _s(tmp_path)
    with s._connect() as db:
        for q in s._STALE_PROBES:
            plan = " ".join(r[3] for r in db.execute("EXPLAIN QUERY PLAN " + q, ("a-", "a.")))
            assert "SCAN" not in plan and "SEARCH" in plan, plan


def test_my_drive_remove_with_no_chunks_left_still_archives(tmp_path):
    """My Drive remove has no drive scoping: a file whose chunks are already gone
    still has its actions archived and links marked (via the stale-id path)."""
    s = _s(tmp_path)
    a = _seed(s, "gdrive-F5-2")
    b = _seed(s, "F5", ent="priya-anand")
    assert s.doc_ids_for_file("F5") == []
    handle_drive_item(FakeDriveService(pages=[]), s, {"ref_id": "F5", "version": "", "event": "remove"})
    assert _act(s, a)["status"] == "auto_archived"
    assert _act(s, b)["status"] == "auto_archived"


# ---------------------------------------------------------------------------
# Final-review fix wave: M1 (batched delete), M3 (per-source removal),
# M7 (mentions on removal)
# ---------------------------------------------------------------------------

def _raw_chunks(s, rows):
    """Insert chunk rows directly (fast; no vec/fts mirrors needed)."""
    import json as _json
    with s._connect(write=True) as db:
        db.executemany(
            "INSERT INTO chunks(doc_id, text, content_hash, metadata) VALUES (?,?,?,?)",
            [(d, "x", "h-" + d, _json.dumps(m)) for d, m in rows])


def test_delete_chunk_rows_batches_past_variable_limit(tmp_path):
    s = _s(tmp_path)
    ids = [f"gdrive-BIG-{i}" for i in range(33000)]
    _raw_chunks(s, [(d, {"source_type": "gdrive", "file_id": "BIG"}) for d in ids])
    assert s.delete_chunks(ids) == 33000
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] == 0


def _rel(s, a, b, rel, src, sources=()):
    from mcpbrain import graph_write as gw
    for e in (a, b):
        if s.get_entity(e) is None:
            s.upsert_entity(e, e, "person", "", "2026-01-01")
    with s._connect(write=True) as db:
        rid = db.execute(
            "INSERT INTO entity_relations(entity_a, relation, entity_b, source_doc_id, "
            "strength, valid_from) VALUES (?,?,?,?,?, '2026-01-01')",
            (a, rel, b, src, max(1, len(sources)))).lastrowid
        for key, cls in sources:
            gw.record_relation_source(db, rid, key, cls)
    return rid


def _relrow(s, rid):
    with s._connect() as db:
        r = dict(db.execute("SELECT strength, invalidated_at FROM entity_relations "
                            "WHERE id=?", (rid,)).fetchone())
        r["keys"] = sorted(x[0] for x in db.execute(
            "SELECT source_key FROM relation_sources WHERE relation_id=?", (rid,)))
    return r


def test_removing_first_source_keeps_relation_with_other_evidence(tmp_path):
    """M3(a): the relation's source_doc_id is the removed file, but five email
    threads also support it: it stays live, the file's support row goes and
    strength is recounted."""
    s = _s(tmp_path)
    _raw_chunks(s, [("gdrive-F1-0", {"source_type": "gdrive", "file_id": "F1"})])
    srcs = [("F1", "primary_doc")] + [(f"t{i}", "primary_mail") for i in range(5)]
    rid = _rel(s, "a", "b", "knows", "gdrive-F1-0", srcs)
    s.invalidate_sources_for_docs(["gdrive-F1-0"], source_ids=["F1"], file_ids=["F1"])
    r = _relrow(s, rid)
    assert r["invalidated_at"] is None
    assert r["strength"] == 5
    assert "F1" not in r["keys"]


def test_removing_later_source_drops_its_support_row(tmp_path):
    """M3(b): the removed file is a LATER source (not source_doc_id): its
    support row goes and strength drops; the relation stays."""
    s = _s(tmp_path)
    _raw_chunks(s, [("gdrive-F2-0", {"source_type": "gdrive", "file_id": "F2"}),
                    ("gmail-m1-body-0", {"source_type": "gmail", "thread_id": "t1",
                                         "message_id": "m1"})])
    rid = _rel(s, "a", "b", "knows", "gmail-m1-body-0",
               [("t1", "primary_mail"), ("F2", "primary_doc")])
    s.invalidate_sources_for_docs(["gdrive-F2-0"], source_ids=["F2"], file_ids=["F2"])
    r = _relrow(s, rid)
    assert (r["invalidated_at"], r["strength"], r["keys"]) == (None, 1, ["t1"])


def test_removing_only_evidence_invalidates(tmp_path):
    s = _s(tmp_path)
    _raw_chunks(s, [("gdrive-F3-0", {"source_type": "gdrive", "file_id": "F3"})])
    rid = _rel(s, "a", "b", "knows", "gdrive-F3-0",
               [("F3", "primary_doc"), ("note-x", "session")])
    legacy = _rel(s, "a", "c", "knows", "gdrive-F3-0")          # no source rows
    s.invalidate_sources_for_docs(["gdrive-F3-0"], source_ids=["F3"], file_ids=["F3"])
    assert _relrow(s, rid)["invalidated_at"] is not None
    assert _relrow(s, rid)["keys"] == ["note-x"]                # derived row stays
    assert _relrow(s, legacy)["invalidated_at"] is not None


def test_partial_removal_keeps_source_key_of_live_thread(tmp_path):
    """A Gmail message deleted from a thread that lives on: the THREAD is
    still evidence, so its support row stays and the relation stays live."""
    s = _s(tmp_path)
    _raw_chunks(s, [("gmail-m1-body-0", {"source_type": "gmail", "thread_id": "t9",
                                         "message_id": "m1"}),
                    ("gmail-m2-body-0", {"source_type": "gmail", "thread_id": "t9",
                                         "message_id": "m2"})])
    rid = _rel(s, "a", "b", "knows", "gmail-m1-body-0", [("t9", "primary_mail")])
    s.invalidate_local_relations_for_docs(["gmail-m1-body-0"], reason="source_gone")
    r = _relrow(s, rid)
    assert (r["invalidated_at"], r["strength"], r["keys"]) == (None, 1, ["t9"])


def test_source_key_removal_is_index_backed(tmp_path):
    s = _s(tmp_path)
    with s._connect() as db:
        plan = " ".join(r[-1] for r in db.execute(
            "EXPLAIN QUERY PLAN SELECT relation_id FROM relation_sources "
            "WHERE source_key IN (?, ?)", ("a", "b")))
    assert "SEARCH" in plan and "idx_relation_sources_key" in plan, plan


def test_removal_decrements_meeting_mentions_and_revive_does_not_inflate(tmp_path):
    """M7: a meeting-series 'about' link counted a mention; removal takes the
    mention back, so remove -> re-link -> remove cycles do not inflate it."""
    from mcpbrain import graph_write as gw
    s = _s(tmp_path)
    s.upsert_entity("meeting-x", "Weekly", "meeting", "", "2026-01-01", bump_mentions=False)
    for cycle in range(3):
        new = s.link_email_entity("m-1", "meeting-x", role="about")
        assert new
        gw._bump_email_count(s, "meeting-x", mentions=True)
        assert s.get_entity("meeting-x")["mentions"] == 1, cycle
        s.invalidate_sources_for_docs([], source_ids=["m-1"], reason="source_gone")
        assert s.get_entity("meeting-x")["mentions"] == 0, cycle
