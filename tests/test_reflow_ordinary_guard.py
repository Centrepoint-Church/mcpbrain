"""Final review I2: the ORDINARY ingest paths must not re-chunk an unchanged
source that is a reflow candidate -- they would upsert enriched=0 over reused
positional ids and delete the tail without invalidating relations. They hand
such an owner to `reflow:<source>` (the carry-over) instead. Real Store."""
import pytest

from mcpbrain import reflow
from mcpbrain.store import Store

PDF = "application/pdf"
M = "2026-01-01T00:00:00Z"


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def _seed_pdf(s, fid="F", modified=M, extra=None):
    for i, t in enumerate(("Budget line one", "Line two")):
        s.upsert_chunk(f"gdrive-{fid}-{i}", t, f"h{i}",
                       {"source_type": "gdrive", "file_id": fid, "mime_type": PDF,
                        "modified": modified, "chunk_index": i, "chunk_total": 2,
                        **(extra or {})})
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET enriched=1")


class _Svc:
    def __init__(self, modified):
        self.modified = modified

    def files(self):
        return self

    def get(self, **kw):
        m = self.modified

        class R:
            def execute(self, num_retries=0):
                return {"id": kw["fileId"], "name": "r.pdf", "mimeType": PDF,
                        "modifiedTime": m, "parents": [], "version": "1"}
        return R()


def _queued(s):
    with s._connect() as db:
        return {(r[0], r[1], r[2]) for r in db.execute(
            "SELECT source, ref_id, modified_at FROM sync_queue")}


def _state(s, fid="F"):
    return [(r["doc_id"], r["text"], r["enriched"]) for r in s.owner_chunks([f"gdrive-{fid}-"])]


def _no_fetch(monkeypatch):
    from mcpbrain.sync import drive
    monkeypatch.setattr(drive, "fetch_content",
                        lambda *a, **k: pytest.fail("re-extracted an unchanged reflow candidate"))


def test_needs_reflow_mirrors_the_selector(tmp_path):
    s = _store(tmp_path)
    _seed_pdf(s)
    assert reflow.needs_reflow(s.owner_chunks(["gdrive-F-"]))
    _seed_pdf(s, fid="G", extra={"extraction_version": 1, "split_version": 1})
    assert not reflow.needs_reflow(s.owner_chunks(["gdrive-G-"]))


def test_my_drive_metadata_only_event_hands_off_to_reflow(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path)
    _seed_pdf(s)
    before = _state(s)
    _no_fetch(monkeypatch)
    drive.handle_drive_item(_Svc(M), s, {"ref_id": "F", "event": "upsert"})
    assert _state(s) == before
    assert ("reflow:drive", "F", "1970-01-01T00:00:00") in _queued(s)


def test_my_drive_changed_file_takes_todays_path(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path)
    _seed_pdf(s)
    monkeypatch.setattr(drive, "fetch_content",
                        lambda *a, **k: drive.Content(text="Brand new text"))
    monkeypatch.setattr(drive, "folder_path", lambda *a, **k: "")
    drive.handle_drive_item(_Svc("2026-06-06T00:00:00Z"), s, {"ref_id": "F", "event": "upsert"})
    assert [t for _d, t, _e in _state(s)] == ["Brand new text"]
    assert not _queued(s)


def test_current_file_is_not_handed_off(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path)
    _seed_pdf(s, extra={"extraction_version": 1, "split_version": 1})
    monkeypatch.setattr(drive, "fetch_content", lambda *a, **k: None)
    drive.handle_drive_item(_Svc(M), s, {"ref_id": "F", "event": "upsert"})
    assert not _queued(s)


def test_stamped_unchanged_file_is_left_alone(tmp_path, monkeypatch):
    """gave_up/unsupported stamps make an owner current for the selector; an
    unchanged source must still not be re-chunked destructively."""
    from mcpbrain.sync import drive
    s = _store(tmp_path)
    _seed_pdf(s, extra={"extraction_version": 1, "split_version": 1,
                        "reflow_skipped": "gave_up"})
    before = _state(s)
    _no_fetch(monkeypatch)
    drive.handle_drive_item(_Svc(M), s, {"ref_id": "F", "event": "upsert"})
    assert _state(s) == before and not _queued(s)


def test_shared_drive_metadata_only_event_hands_off_to_reflow(tmp_path, monkeypatch):
    from mcpbrain.org_contracts import FleetPin
    from mcpbrain.sync import drive
    from tests.helpers.org_fleet import LocalDirFleetStorage
    s = _store(tmp_path)
    _seed_pdf(s, extra={"drive_id": "D1"})
    before = _state(s)
    _no_fetch(monkeypatch)
    pin = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
                   enrich_logic_floor=1, fleet_secret="s3cret")
    drive.handle_shared_drive_item(_Svc(M), s, {"ref_id": "F", "event": "upsert"},
                                   fleet_storage=LocalDirFleetStorage(tmp_path / "fs"),
                                   pin=pin, drive_id="D1")
    assert _state(s) == before
    assert ("reflow:drive", "F", "1970-01-01T00:00:00") in _queued(s)
    assert s.pending_publishes("D1") == []


def test_backfill_drive_skips_unchanged_reflow_candidates(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path)
    _seed_pdf(s)
    before = _state(s)
    _no_fetch(monkeypatch)

    class _List:
        def files(self):
            return self

        def list(self, **kw):
            class R:
                def execute(self, num_retries=0):
                    return {"files": [{"id": "F", "name": "r.pdf", "mimeType": PDF,
                                       "modifiedTime": M, "parents": []}]}
            return R()
    assert drive.backfill_drive(_List(), s, "2000-01-01T00:00:00Z") == 0
    assert _state(s) == before
    assert ("reflow:drive", "F", "1970-01-01T00:00:00") in _queued(s)


def _event(desc):
    return {"id": "E", "status": "confirmed", "summary": "Board meeting",
            "start": {"dateTime": "2026-02-01T10:00:00Z"},
            "end": {"dateTime": "2026-02-01T11:00:00Z"}, "description": desc}


class _Cal:
    def __init__(self, ev):
        self.ev = ev

    def events(self):
        return self

    def get(self, **kw):
        ev = self.ev

        class R:
            def execute(self, num_retries=0):
                return ev
        return R()


def _seed_calendar_v0(s, ev):
    """The event as the OLD word-splitting chunker stored it."""
    from mcpbrain.sync import calendar
    from tests.oracles.chunking_v0 import chunk_text_v0
    text = "\n\n".join(c.text for c in calendar.normalise_calendar(ev))
    parts = chunk_text_v0(text)
    assert len(parts) > 1
    for i, t in enumerate(parts):
        s.upsert_chunk(f"cal-E-{i}", t, f"c{i}",
                       {"source_type": "calendar", "event_id": "E", "chunk_index": i,
                        "chunk_total": len(parts)})
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET enriched=1")


def _long_desc():
    return "\n".join(f"Agenda item {i}: review the Northgate Trust budget line {i} "
                     "with Dana Okafor and Marcus Reyes before the vote." for i in range(60))


def test_calendar_unchanged_stale_event_hands_off_to_reflow(tmp_path, monkeypatch):
    from mcpbrain.sync import calendar
    s = _store(tmp_path)
    ev = _event(_long_desc())
    _seed_calendar_v0(s, ev)
    before = [(r["doc_id"], r["text"], r["enriched"]) for r in s.owner_chunks(["cal-E"])]
    monkeypatch.setattr(calendar, "owner_identity_from_config", lambda: None)
    calendar.handle_calendar_item(_Cal(ev), s, {"ref_id": "E", "event": "upsert"})
    after = [(r["doc_id"], r["text"], r["enriched"]) for r in s.owner_chunks(["cal-E"])]
    assert after == before
    assert ("reflow:calendar", "E", "1970-01-01T00:00:00") in _queued(s)


def test_calendar_changed_event_takes_todays_path(tmp_path, monkeypatch):
    from mcpbrain.sync import calendar
    s = _store(tmp_path)
    _seed_calendar_v0(s, _event(_long_desc()))
    monkeypatch.setattr(calendar, "owner_identity_from_config", lambda: None)
    calendar.handle_calendar_item(_Cal(_event("Moved to the hall.")), s,
                                  {"ref_id": "E", "event": "upsert"})
    assert "Moved to the hall." in s.get_chunk("cal-E")["text"]
    assert not _queued(s)


def test_repair_reingest_stale_skips_reflow_owners(tmp_path, monkeypatch, capsys):
    import bin.repair as repair
    s = _store(tmp_path)
    _seed_pdf(s)                                    # a reflow candidate, chunker v0
    for i in range(2):                              # stale, not a reflow candidate
        s.upsert_chunk(f"gdrive-T-{i}", f"cell {i}", f"t{i}",
                       {"source_type": "gdrive", "file_id": "T", "mime_type": "text/csv",
                        "content_subtype": "table", "chunk_index": i, "chunk_total": 2})
    seen = {}

    def _fake_reingest_files(service, store, ids, **kw):
        seen["ids"] = list(ids)
        return {"files": 0, "missing": 0, "failed": 0, "orphans": 0}
    monkeypatch.setattr("mcpbrain.auth.build_google_services",
                        lambda: {"drive_service": object()})
    monkeypatch.setattr("mcpbrain.sync.drive.reingest_files", _fake_reingest_files)
    repair.phase_reingest_stale(s, True, limit=500)
    assert seen["ids"] == ["T"]
    assert "belong to the reflow" in capsys.readouterr().out


def test_calendar_window_backfill_hands_off_unchanged_stale_events(tmp_path, monkeypatch):
    """The full re-sync after a 410 and repair's window backfill go through
    backfill_calendar_window: the same guard applies."""
    from mcpbrain.sync import calendar
    s = _store(tmp_path)
    ev = _event(_long_desc())
    _seed_calendar_v0(s, ev)
    before = [(r["doc_id"], r["text"], r["enriched"]) for r in s.owner_chunks(["cal-E"])]
    monkeypatch.setattr(calendar, "owner_identity_from_config", lambda: None)

    class _ListCal:
        def events(self):
            return self

        def list(self, **kw):
            class R:
                def execute(self, num_retries=0):
                    return {"items": [ev]}
            return R()
    calendar.backfill_calendar_window(_ListCal(), s, time_min="2026-01-01T00:00:00Z",
                                      time_max="2026-12-31T00:00:00Z")
    assert [(r["doc_id"], r["text"], r["enriched"]) for r in s.owner_chunks(["cal-E"])] == before
    assert ("reflow:calendar", "E", "1970-01-01T00:00:00") in _queued(s)


# -- residual R6: a stamped, unchanged owner still gets its metadata refreshed

def _meta(s, fid="F"):
    return [r["metadata"] for r in s.owner_chunks([f"gdrive-{fid}-"])]


def test_stamped_unchanged_file_gets_renamed_and_moved_metadata(tmp_path, monkeypatch):
    """A rename/move is a metadata-only Drive event: the re-chunk is skipped
    (stamped owner) but file_name and folder_path must still follow it, as the
    ordinary path's patch_chunk_metadata does for any content-unchanged file."""
    from mcpbrain.sync import drive
    s = _store(tmp_path)
    _seed_pdf(s, extra={"extraction_version": 1, "split_version": 1,
                        "reflow_skipped": "gave_up", "file_name": "old.pdf",
                        "folder_path": "Old"})
    before = _state(s)
    _no_fetch(monkeypatch)
    monkeypatch.setattr(drive, "folder_path", lambda *a, **k: "Finance/Budgets")
    drive.handle_drive_item(_Svc(M), s, {"ref_id": "F", "event": "upsert"})
    assert _state(s) == before and not _queued(s)
    for m in _meta(s):
        assert (m["file_name"], m["folder_path"], m["modified"]) == ("r.pdf", "Finance/Budgets", M)
        assert m["reflow_skipped"] == "gave_up"


def test_backfill_refreshes_a_stamped_owners_metadata(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path)
    _seed_pdf(s, extra={"extraction_version": 1, "split_version": 1,
                        "reflow_skipped": "unsupported", "file_name": "old.pdf"})
    _no_fetch(monkeypatch)
    monkeypatch.setattr(drive, "folder_path", lambda *a, **k: "Board")

    class _List:
        def files(self):
            return self

        def list(self, **kw):
            class R:
                def execute(self, num_retries=0):
                    return {"files": [{"id": "F", "name": "new.pdf", "mimeType": PDF,
                                       "modifiedTime": M, "parents": ["P"]}]}
            return R()

    assert drive.backfill_drive(_List(), s, "2000-01-01T00:00:00Z") == 0
    assert {(m["file_name"], m.get("folder_path")) for m in _meta(s)} == {("new.pdf", "Board")}
