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
