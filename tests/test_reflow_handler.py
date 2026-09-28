"""Task 11 (2026-09-24 extraction-fidelity): the reflow queue handler, DEFER in
work_queue, and the cycle wiring. Real Store throughout."""
import json
import os

import pytest

from mcpbrain.store import ReflowOrphanError, Store
from mcpbrain.sync import queue
from mcpbrain.sync.normalise import Chunk
from mcpbrain.sync.reflow_handler import HALT_CURSOR, ReflowContext

PDF = "application/pdf"


class _Emb:
    dim = 4
    def embed_passages(self, xs):
        return [[0.1, 0.2, 0.3, 0.4] for _ in xs]


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4); s.init(); return s


def _enrich(s, *doc_ids):
    with s._connect(write=True) as db:
        for d in doc_ids:
            db.execute("UPDATE chunks SET enriched=1 WHERE doc_id=?", (d,))


def _seed_drive(s, fid="F", modified="2026-01-01T00:00:00Z", texts=("Budget Line one", "Line two"),
                extra=None):
    for i, t in enumerate(texts):
        s.upsert_chunk(f"gdrive-{fid}-{i}", t, f"h{i}",
                       {"source_type": "gdrive", "file_id": fid, "mime_type": PDF,
                        "modified": modified, "chunk_index": i, "chunk_total": len(texts),
                        **(extra or {})})
        _enrich(s, f"gdrive-{fid}-{i}")


class _DriveSvc:
    """files().get returns fmeta; fetch_content is monkeypatched."""
    def __init__(self, modified, exc=None):
        self.modified, self.exc = modified, exc
    def files(self):
        return self
    def get(self, **kw):
        m, exc = self.modified, self.exc
        class R:
            def execute(self, num_retries=0):
                if exc:
                    raise exc
                return {"id": kw["fileId"], "name": "r.pdf", "mimeType": PDF,
                        "modifiedTime": m, "parents": []}
        return R()


def _ctx(s, tmp_path, **kw):
    return ReflowContext(s, _Emb(), str(tmp_path), **kw)


def _http_error(status):
    from googleapiclient.errors import HttpError

    class _Resp(dict):
        pass
    r = _Resp(); r.status = status; r.reason = "x"
    return HttpError(r, b"{}")


def _drive_blocks(monkeypatch):
    from mcpbrain.sync import drive
    from mcpbrain.sync.blocks import Heading, Paragraph
    monkeypatch.setattr(drive, "fetch_content", lambda svc, fm, **k: drive.Content(
        text="Budget\n\nLine one\nLine two",
        blocks=[Heading(1, "Budget"), Paragraph("Line one\nLine two")]))
    monkeypatch.setattr(drive, "folder_path", lambda *a, **k: "")


# ---- brief tests -----------------------------------------------------------

def test_reflow_drive_unchanged_carries_over(tmp_path, monkeypatch):
    s = _store(tmp_path); _seed_drive(s)
    _drive_blocks(monkeypatch)
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is None
    rows = s.owner_chunks(["gdrive-F-"])
    assert len(rows) == 1 and rows[0]["enriched"] == 1
    assert rows[0]["metadata"]["extraction_version"] == 1
    assert rows[0]["metadata"]["heading_trail"] == "Budget"
    assert s.reflow_stats()["owners_done"] == 1


def test_reflow_drive_changed_file_takes_normal_path(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path); _seed_drive(s)
    called = {}
    monkeypatch.setattr(drive, "handle_drive_item",
                        lambda svc, st, item, **k: called.setdefault("item", item))
    monkeypatch.setattr(s, "apply_reflow", lambda *a, **k: pytest.fail("reached apply_reflow"))
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-05-05T00:00:00Z"))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is None
    assert called["item"]["event"] == "upsert" and called["item"]["ref_id"] == "F"


def test_reflow_empty_extraction_fails_without_deleting(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path); _seed_drive(s)
    monkeypatch.setattr(drive, "fetch_content", lambda *a, **k: drive.Content())
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    with pytest.raises(RuntimeError):
        ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0})
    assert len(s.owner_chunks(["gdrive-F-"])) == 2


def test_reflow_gmail_404_stamps_and_completes(tmp_path, monkeypatch):
    from mcpbrain.sync import gmail
    s = _store(tmp_path)
    s.upsert_chunk("gmail-M-body-0", "hi", "h", {"source_type": "gmail", "message_id": "M",
                   "chunk_index": 0, "chunk_total": 2})
    monkeypatch.setattr(gmail, "_fetch_one", lambda *a, **k: (None, []))
    ctx = _ctx(s, tmp_path, gmail_service=object())
    assert ctx.handle({"source": "reflow:gmail", "ref_id": "M", "attempts": 0}) is None
    md = s.owner_chunks(["gmail-M-"])[0]["metadata"]
    assert md["split_version"] == 1 and md["reflow_skipped"] == "source_gone"


def _seed_gmail_with_pdf(s):
    s.upsert_chunk("gmail-M-body-0", "hello there friend", "b0",
                   {"source_type": "gmail", "message_id": "M", "chunk_index": 0, "chunk_total": 1})
    s.upsert_chunk("gmail-M-att-0-0", "Item Cost Chairs 120", "a0",
                   {"source_type": "gmail", "message_id": "M", "attachment_mime": PDF,
                    "content_type": "email_attachment", "chunk_index": 0, "chunk_total": 1})
    _enrich(s, "gmail-M-body-0", "gmail-M-att-0-0")


def _gmail_body():
    return Chunk("gmail-M-body-0", "hello there friend", "b0",
                 {"source_type": "gmail", "message_id": "M", "chunk_index": 0, "chunk_total": 1,
                  "split_version": 1}, ["hello there friend"])


def test_reflow_gmail_changed_pdf_attachment_still_carries_over(tmp_path, monkeypatch):
    """A block-extracted attachment's text is EXPECTED to differ; that must not
    be read as 'the message changed' (which would skip the carry-over)."""
    from mcpbrain.sync import gmail
    import mcpbrain.sync.normalise as nm
    s = _store(tmp_path); _seed_gmail_with_pdf(s)
    att = Chunk("gmail-M-att-0-0", "Table\nItem: Chairs; Cost: 120", "a1",
                {"source_type": "gmail", "message_id": "M", "attachment_mime": PDF,
                 "extraction_version": 1, "chunk_index": 0, "chunk_total": 1},
                ["Item", "Cost", "Chairs", "120"])
    monkeypatch.setattr(gmail, "_fetch_one", lambda *a, **k: ({"id": "M"}, [att]))
    monkeypatch.setattr(nm, "normalise_gmail", lambda raw, **k: [_gmail_body()])
    called = {}
    monkeypatch.setattr(gmail, "handle_gmail_item", lambda *a, **k: called.setdefault("n", 1))
    ctx = _ctx(s, tmp_path, gmail_service=object())
    assert ctx.handle({"source": "reflow:gmail", "ref_id": "M", "attempts": 0}) is None
    assert "n" not in called                       # did NOT take the ordinary path
    rows = {r["doc_id"]: r for r in s.owner_chunks(["gmail-M-"])}
    assert rows["gmail-M-att-0-0"]["metadata"]["extraction_version"] == 1
    assert rows["gmail-M-att-0-0"]["enriched"] == 1   # every cell was already extracted


def test_cap_defers_after_max_items(tmp_path):
    s = _store(tmp_path)
    ctx = _ctx(s, tmp_path, max_items=0)
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is queue.DEFER


def test_halted_defers(tmp_path):
    s = _store(tmp_path)
    s.set_cursor(HALT_CURSOR, "orphan in X")
    assert _ctx(s, tmp_path).handle({"source": "reflow:drive", "ref_id": "F",
                                     "attempts": 0}) is queue.DEFER


def test_owner_in_pending_enrich_unit_defers(tmp_path):
    s = _store(tmp_path); _seed_drive(s)
    units = tmp_path / "enrich_queue" / "units"; os.makedirs(units)
    (units / "u-1.json").write_text(json.dumps({"unit_id": "u-1", "kind": "thread",
        "threads": [{"thread_id": "F", "messages": [{"message_id": "F",
                     "chunk_doc_ids": ["gdrive-F-0"]}]}]}))
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is queue.DEFER


def test_work_queue_leaves_deferred_rows(tmp_path):
    s = _store(tmp_path)
    s.enqueue_items([{"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:drive")
    out = queue.work_queue(s, handlers={"reflow": lambda it: queue.DEFER}, limit=5)
    assert out == {"processed": 0, "failed": 0}
    assert s.reflow_stats()["queued"] == 1


# ---- extras ----------------------------------------------------------------

def test_work_queue_defer_leaves_attempts_and_backoff_untouched(tmp_path):
    s = _store(tmp_path)
    s.enqueue_items([{"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:drive")
    for _ in range(3):
        queue.work_queue(s, handlers={"reflow": lambda it: queue.DEFER}, limit=5)
    row = s.due_sync_items(limit=5, now="2099-01-01T00:00:00")[0]
    assert row["attempts"] == 0 and row["next_attempt_at"] is None and not row["last_error"]


def test_claimed_unit_is_also_guarded_and_rescanned_mid_cycle(tmp_path, monkeypatch):
    """A unit written (or claimed) AFTER the first scan is still seen: the
    guard re-scans when enrich_queue/units or claims changes."""
    s = _store(tmp_path); _seed_drive(s, "F"); _seed_drive(s, "G")
    _drive_blocks(monkeypatch)
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is None
    q = tmp_path / "enrich_queue"
    os.makedirs(q / "units"); os.makedirs(q / "claims")
    # G is named only through a chunk doc_id, in a unit that is claimed.
    (q / "units" / "u-2.json").write_text(json.dumps({"unit_id": "u-2", "kind": "thread",
        "threads": [{"thread_id": "T", "part_doc_ids": ["gdrive-G-1"], "messages": []}]}))
    (q / "claims" / "u-2").write_text("")
    assert ctx.handle({"source": "reflow:drive", "ref_id": "G", "attempts": 0}) is queue.DEFER
    assert len(s.owner_chunks(["gdrive-G-"])) == 2


def test_gmail_attachment_refetch_failure_refuses_and_keeps_chunks(tmp_path, monkeypatch):
    """The attachment fetch failing (fetch_and_normalise swallows it) leaves the
    attachment's lineage with no new chunks. That must NOT be read as a source
    change (the ordinary path would re-ingest without it) nor applied (it would
    delete it): apply_reflow refuses with ValueError, which propagates so
    work_queue backs the item off."""
    from mcpbrain.sync import gmail
    import mcpbrain.sync.normalise as nm
    s = _store(tmp_path); _seed_gmail_with_pdf(s)
    monkeypatch.setattr(gmail, "_fetch_one", lambda *a, **k: ({"id": "M"}, []))
    monkeypatch.setattr(nm, "normalise_gmail", lambda raw, **k: [_gmail_body()])
    monkeypatch.setattr(gmail, "handle_gmail_item",
                        lambda *a, **k: pytest.fail("took the ordinary path"))
    ctx = _ctx(s, tmp_path, gmail_service=object())
    with pytest.raises(ValueError, match="lineage"):
        ctx.handle({"source": "reflow:gmail", "ref_id": "M", "attempts": 0})
    ids = {r["doc_id"] for r in s.owner_chunks(["gmail-M-"])}
    assert ids == {"gmail-M-body-0", "gmail-M-att-0-0"}


def test_gmail_lineage_gone_backs_off_through_work_queue_then_gives_up(tmp_path, monkeypatch):
    from mcpbrain.sync import gmail
    import mcpbrain.sync.normalise as nm
    s = _store(tmp_path); _seed_gmail_with_pdf(s)
    monkeypatch.setattr(gmail, "_fetch_one", lambda *a, **k: ({"id": "M"}, []))
    monkeypatch.setattr(nm, "normalise_gmail", lambda raw, **k: [_gmail_body()])
    s.enqueue_items([{"ref_id": "M", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:gmail")
    for year in range(2090, 2095):          # each pass past the previous backoff
        ctx = _ctx(s, tmp_path, gmail_service=object())
        out = queue.work_queue(s, handlers={"reflow": ctx.handle}, limit=5,
                               now=f"{year}-01-01T00:00:00")
        assert out == {"processed": 0, "failed": 1}
    ctx = _ctx(s, tmp_path, gmail_service=object())
    out = queue.work_queue(s, handlers={"reflow": ctx.handle}, limit=5,
                           now="2100-01-01T00:00:00")
    assert out == {"processed": 1, "failed": 0}
    rows = s.owner_chunks(["gmail-M-"])
    assert len(rows) == 2                               # nothing deleted, ever
    assert all(r["metadata"]["reflow_skipped"] == "gave_up" for r in rows)
    assert rows[1]["metadata"]["extraction_version"] == 1   # selector stops matching
    assert s.reflow_stats()["queued"] == 0


def test_gmail_body_text_changed_takes_ordinary_path(tmp_path, monkeypatch):
    from mcpbrain.sync import gmail
    import mcpbrain.sync.normalise as nm
    s = _store(tmp_path); _seed_gmail_with_pdf(s)
    body = _gmail_body(); body.text = "entirely different words"
    att = Chunk("gmail-M-att-0-0", "Item Cost Chairs 120", "a0",
                {"source_type": "gmail", "message_id": "M", "attachment_mime": PDF,
                 "chunk_index": 0, "chunk_total": 1}, ["Item Cost Chairs 120"])
    monkeypatch.setattr(gmail, "_fetch_one", lambda *a, **k: ({"id": "M"}, [att]))
    monkeypatch.setattr(nm, "normalise_gmail", lambda raw, **k: [body])
    called = {}
    monkeypatch.setattr(gmail, "handle_gmail_item",
                        lambda svc, st, item, **k: called.update(item=item, **k))
    monkeypatch.setattr(s, "apply_reflow", lambda *a, **k: pytest.fail("reached apply_reflow"))
    ctx = _ctx(s, tmp_path, gmail_service=object())
    assert ctx.handle({"source": "reflow:gmail", "ref_id": "M", "attempts": 0}) is None
    assert called["item"]["ref_id"] == "M" and called["fetch_attachments"] is True


def test_partial_attachment_reextraction_raises(tmp_path, monkeypatch):
    from mcpbrain.sync import attachments, gmail
    from mcpbrain.sync.blocks import PartialBlocks, Paragraph
    import mcpbrain.sync.normalise as nm
    s = _store(tmp_path); _seed_gmail_with_pdf(s)
    monkeypatch.setitem(attachments._BLOCK_EXTRACTORS, PDF,
                        lambda _b: PartialBlocks([Paragraph("Item Cost")]))
    raw = {"id": "M", "threadId": "T", "payload": {"headers": []}}
    att = attachments.normalise_attachment(raw, {"filename": "i.pdf", "mime": PDF, "index": 0},
                                           b"%PDF")
    assert att[0].metadata["extraction_partial"] is True
    monkeypatch.setattr(gmail, "_fetch_one", lambda *a, **k: ({"id": "M"}, att))
    monkeypatch.setattr(nm, "normalise_gmail", lambda raw, **k: [_gmail_body()])
    with pytest.raises(RuntimeError, match="partial"):
        _ctx(s, tmp_path, gmail_service=object()).handle(
            {"source": "reflow:gmail", "ref_id": "M", "attempts": 0})
    assert s.get_chunk("gmail-M-att-0-0")["text"] == "Item Cost Chairs 120"


def test_partial_drive_reextraction_raises_without_deleting(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path); _seed_drive(s)
    monkeypatch.setattr(drive, "fetch_content",
                        lambda *a, **k: drive.Content(text="Budget", partial=True))
    with pytest.raises(RuntimeError):
        _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z")).handle(
            {"source": "reflow:drive", "ref_id": "F", "attempts": 0})
    assert len(s.owner_chunks(["gdrive-F-"])) == 2


def test_orphan_error_halts_the_cadence(tmp_path, monkeypatch):
    s = _store(tmp_path); _seed_drive(s); _seed_drive(s, "G")
    _drive_blocks(monkeypatch)

    def _boom(*a, **k):
        raise ReflowOrphanError("reflow F: 1 dangling reference(s)")
    monkeypatch.setattr(s, "apply_reflow", _boom)
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    with pytest.raises(ReflowOrphanError):
        ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0})
    assert "dangling" in s.get_cursor(HALT_CURSOR)
    assert ctx.handle({"source": "reflow:drive", "ref_id": "G", "attempts": 0}) is queue.DEFER


def test_drive_404_stamps_source_gone(tmp_path):
    s = _store(tmp_path); _seed_drive(s)
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("x", exc=_http_error(404)))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is None
    rows = s.owner_chunks(["gdrive-F-"])
    assert len(rows) == 2
    assert all(r["metadata"]["reflow_skipped"] == "source_gone"
               and r["metadata"]["extraction_version"] == 1 for r in rows)


def test_drive_transient_error_raises(tmp_path):
    s = _store(tmp_path); _seed_drive(s)
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("x", exc=_http_error(500)))
    with pytest.raises(Exception):
        ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0})


def test_gives_up_at_five_attempts(tmp_path, monkeypatch):
    from mcpbrain.sync import drive
    s = _store(tmp_path); _seed_drive(s)
    monkeypatch.setattr(drive, "fetch_content", lambda *a, **k: pytest.fail("fetched"))
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 5}) is None
    assert all(r["metadata"]["reflow_skipped"] == "gave_up"
               for r in s.owner_chunks(["gdrive-F-"]))


def test_shared_drive_changed_file_goes_to_the_shared_drive_handler(tmp_path):
    from mcpbrain.org_contracts import DRIVE_ID_META_KEY
    s = _store(tmp_path); _seed_drive(s, extra={DRIVE_ID_META_KEY: "D1"})
    seen = []
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-05-05T00:00:00Z"),
               normal_handlers={"drive": seen.append})
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is None
    assert seen[0]["source"] == "drive:D1" and seen[0]["event"] == "upsert"


def test_shared_drive_changed_file_without_cycle_handler_raises(tmp_path):
    """Never re-ingest a Shared Drive file through the My-Drive handler: it
    would drop the drive_id stamp revocation depends on."""
    from mcpbrain.org_contracts import DRIVE_ID_META_KEY
    s = _store(tmp_path); _seed_drive(s, extra={DRIVE_ID_META_KEY: "D1"})
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-05-05T00:00:00Z"))
    with pytest.raises(RuntimeError, match="shared-drive"):
        ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0})


def test_shared_drive_unchanged_reflow_records_a_pending_publish(tmp_path, monkeypatch):
    from mcpbrain.org_contracts import DRIVE_ID_META_KEY
    s = _store(tmp_path); _seed_drive(s, extra={DRIVE_ID_META_KEY: "D1"})
    _drive_blocks(monkeypatch)
    ctx = _ctx(s, tmp_path, drive_service=_DriveSvc("2026-01-01T00:00:00Z"))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is None
    assert s.owner_chunks(["gdrive-F-"])[0]["metadata"][DRIVE_ID_META_KEY] == "D1"
    assert [p[0] if isinstance(p, tuple) else p["file_id"]
            for p in s.pending_publishes("D1")] == ["F"]


def test_missing_service_defers(tmp_path):
    s = _store(tmp_path); _seed_drive(s)
    assert _ctx(s, tmp_path).handle({"source": "reflow:drive", "ref_id": "F",
                                     "attempts": 0}) is queue.DEFER


def test_calendar_unchanged_multi_chunk_event_reflows(tmp_path, monkeypatch):
    from mcpbrain.sync import calendar
    s = _store(tmp_path)
    md = {"source_type": "calendar", "event_id": "E", "summary": "Staff meeting"}
    s.upsert_chunk("cal-E-0", "Staff meeting agenda", "c0", {**md, "chunk_index": 0, "chunk_total": 2})
    s.upsert_chunk("cal-E-1", "second part", "c1", {**md, "chunk_index": 1, "chunk_total": 2})
    _enrich(s, "cal-E-0", "cal-E-1")
    new = Chunk("cal-E", "Staff meeting agenda\nsecond part", "c",
                {**md, "split_version": 1, "chunk_index": 0, "chunk_total": 1})

    class _Cal:
        def events(self):
            return self
        def get(self, **k):
            class R:
                def execute(self, num_retries=0):
                    return {"id": "E"}
            return R()
    monkeypatch.setattr(calendar, "normalise_calendar", lambda ev: [new])
    ctx = _ctx(s, tmp_path, calendar_service=_Cal())
    assert ctx.handle({"source": "reflow:calendar", "ref_id": "E", "attempts": 0}) is None
    rows = s.owner_chunks(["cal-E"])
    assert [r["doc_id"] for r in rows] == ["cal-E"] and rows[0]["enriched"] == 1


def test_calendar_changed_event_takes_ordinary_path(tmp_path, monkeypatch):
    from mcpbrain.sync import calendar
    s = _store(tmp_path)
    md = {"source_type": "calendar", "event_id": "E"}
    s.upsert_chunk("cal-E-0", "old agenda", "c0", {**md, "chunk_index": 0, "chunk_total": 2})
    s.upsert_chunk("cal-E-1", "old part", "c1", {**md, "chunk_index": 1, "chunk_total": 2})
    monkeypatch.setattr(calendar, "normalise_calendar", lambda ev: [
        Chunk("cal-E", "moved to Friday", "c", {**md, "chunk_index": 0, "chunk_total": 1})])
    seen = []

    class _Cal:
        def events(self):
            return self
        def get(self, **k):
            class R:
                def execute(self, num_retries=0):
                    return {"id": "E"}
            return R()
    ctx = _ctx(s, tmp_path, calendar_service=_Cal(), normal_handlers={"calendar": seen.append})
    assert ctx.handle({"source": "reflow:calendar", "ref_id": "E", "attempts": 0}) is None
    assert seen and seen[0]["ref_id"] == "E"
    assert len(s.owner_chunks(["cal-E"])) == 2        # untouched by reflow


def test_anarlog_session_reflows_and_gone_session_stamps(tmp_path, monkeypatch):
    from mcpbrain.sync import anarlog
    s = _store(tmp_path)
    md = {"source_type": "anarlog", "session_id": "S1", "content_subtype": "transcript"}
    s.upsert_chunk("anarlog-S1-transcript-0", "Dana Okafor: we agreed", "t0",
                   {**md, "chunk_index": 0, "chunk_total": 2})
    s.upsert_chunk("anarlog-S1-transcript-1", "the roster", "t1",
                   {**md, "chunk_index": 1, "chunk_total": 2})
    new = [Chunk("anarlog-S1-transcript-0", "Dana Okafor: we agreed the roster", "t",
                 {**md, "split_version": 1, "chunk_index": 0, "chunk_total": 1})]
    db = tmp_path / "anarlog.sqlite"
    db.write_bytes(b"")
    monkeypatch.setattr(anarlog, "read_session", lambda conn, sid: {"id": sid})
    monkeypatch.setattr(anarlog, "normalise_session", lambda sess: new)
    ctx = _ctx(s, tmp_path, anarlog_db=str(db))
    assert ctx.handle({"source": "reflow:anarlog", "ref_id": "S1", "attempts": 0}) is None
    rows = s.owner_chunks(["anarlog-S1-"])
    assert [r["doc_id"] for r in rows] == ["anarlog-S1-transcript-0"]
    assert rows[0]["metadata"]["split_version"] == 1

    monkeypatch.setattr(anarlog, "read_session", lambda conn, sid: None)
    ctx = _ctx(s, tmp_path, anarlog_db=str(db))
    assert ctx.handle({"source": "reflow:anarlog", "ref_id": "S1", "attempts": 0}) is None
    assert s.owner_chunks(["anarlog-S1-"])[0]["metadata"]["reflow_skipped"] == "source_gone"


def test_run_sync_cycle_registers_reflow_and_kill_switch_defers(tmp_path, monkeypatch):
    from mcpbrain import config
    from mcpbrain.sync import run_sync_cycle
    s = _store(tmp_path)
    s.enqueue_items([{"ref_id": "F", "event": "reflow", "modified_at": "1970-01-01T00:00:00"}],
                    source="reflow:drive")
    seen = []
    import mcpbrain.sync.reflow_handler as rh
    monkeypatch.setattr(rh.ReflowContext, "handle",
                        lambda self, it: seen.append(it["ref_id"]) or queue.DEFER)
    monkeypatch.setattr(config, "reflow_enabled", lambda home: True)
    run_sync_cycle(s, _Emb(), home=str(tmp_path))
    assert seen == ["F"]

    monkeypatch.setattr(config, "reflow_enabled", lambda home: False)
    out = run_sync_cycle(s, _Emb(), home=str(tmp_path))
    assert seen == ["F"]                                   # not worked again
    row = s.due_sync_items(limit=5, now="2099-01-01T00:00:00")[0]
    assert row["attempts"] == 0                            # deferred, not failed
    assert out["worked"]["failed"] == 0
