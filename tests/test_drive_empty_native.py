"""A changed Google-native Drive file that now exports empty loses its stale
chunks (and its actions/links, via source_gone); anything else keeps them."""
import pytest

from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from mcpbrain.sync import drive
from mcpbrain.sync.reflow_handler import ReflowContext
from tests.helpers.org_fleet import LocalDirFleetStorage

GDOC = "application/vnd.google-apps.document"
PDF = "application/pdf"
OLD = "2026-01-01T00:00:00Z"
NEW = "2026-06-06T00:00:00Z"
PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _store(tmp_path):
    s = Store(tmp_path / "a.sqlite3", dim=4)
    s.init()
    return s


def _seed(s, fid="F", mime=GDOC, modified=OLD, drive_id=None):
    for i in range(2):
        md = {"source_type": "gdrive", "file_id": fid, "mime_type": mime,
              "modified": modified, "chunk_index": i, "chunk_total": 2}
        if drive_id:
            md["drive_id"] = drive_id
        s.upsert_chunk(f"gdrive-{fid}-{i}", f"Vanished text {i}", f"h{i}", md)


def _action(s, fid="F"):
    s.upsert_entity("dana-okafor", "Dana Okafor", "person", "", "2026-01-01")
    s.link_email_entity(fid, "dana-okafor", role="sender")
    return s.add_unified_action(text="t", source="email", source_doc_id=fid,
                                thread_id=fid)


def _status(s, i):
    with s._connect() as db:
        return db.execute("SELECT status FROM actions WHERE id=?", (i,)).fetchone()[0]


class _Svc:
    def __init__(self, mime, modified):
        self.mime, self.modified = mime, modified

    def files(self):
        return self

    def get(self, **kw):
        m, mt = self.mime, self.modified

        class R:
            def execute(self, num_retries=0):
                return {"id": kw["fileId"], "name": "d", "mimeType": m,
                        "modifiedTime": mt, "parents": [], "version": "2"}
        return R()


def _fetch(monkeypatch, content):
    monkeypatch.setattr(drive, "fetch_content", lambda *a, **k: content)
    monkeypatch.setattr(drive, "folder_path", lambda *a, **k: "")


def _my(s, mime=GDOC, modified=NEW):
    drive.handle_drive_item(_Svc(mime, modified), s, {"ref_id": "F", "event": "upsert"})


def _shared(s, tmp_path, drive_id="D1", mime=GDOC, modified=NEW):
    drive.handle_shared_drive_item(
        _Svc(mime, modified), s, {"ref_id": "F", "event": "upsert"},
        fleet_storage=LocalDirFleetStorage(tmp_path / "fs"), pin=PIN, drive_id=drive_id)


def test_my_drive_emptied_native_drops_chunks_and_archives(tmp_path, monkeypatch):
    s = _store(tmp_path)
    _seed(s)
    a = _action(s, "gdrive-F-0")
    _fetch(monkeypatch, drive.Content(text=""))
    _my(s)
    assert s.owner_chunks(["gdrive-F-"]) == []
    assert _status(s, a) == "auto_archived"


def test_shared_drive_emptied_native_scoped_and_not_published(tmp_path, monkeypatch):
    s = _store(tmp_path)
    _seed(s, drive_id="D1")
    s.record_pending_publish("D1", "F", "oldhash")
    _fetch(monkeypatch, drive.Content(text=""))
    _shared(s, tmp_path, drive_id="D2")                 # other drive: untouched
    assert len(s.owner_chunks(["gdrive-F-"])) == 2
    _shared(s, tmp_path, drive_id="D1")
    assert s.owner_chunks(["gdrive-F-"]) == []
    assert s.pending_publishes("D1") == []


@pytest.mark.parametrize("mime,stored,content", [
    (PDF, OLD, drive.Content(text="")),                          # extraction failure
    (GDOC, NEW, drive.Content(text="")),                         # not changed
    (GDOC, OLD, drive.Content(text="", partial=True)),           # partial
    (GDOC, OLD, None),                                           # unsupported
], ids=["pdf", "unchanged", "partial", "none"])
def test_keep_cases(tmp_path, monkeypatch, mime, stored, content):
    for path in ("my", "shared"):
        (tmp_path / path).mkdir()
        s = _store(tmp_path / path)
        _seed(s, mime=mime, modified=stored, drive_id="D1")
        _fetch(monkeypatch, content)
        if path == "my":
            _my(s, mime=mime)
        else:
            _shared(s, tmp_path, mime=mime)
        assert len(s.owner_chunks(["gdrive-F-"])) == 2, path


def test_reflow_routes_emptied_native_and_owner_no_longer_held(tmp_path, monkeypatch):
    from datetime import datetime, timezone
    s = _store(tmp_path)
    _seed(s)
    with s._connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO reflow_owners(owner, source, at, chunks_new,"
                   " carried, reenrich, outcome) VALUES('F','drive',?,0,0,0,'ordinary')",
                   (datetime.now(timezone.utc).isoformat(),))
    assert s.reflow_live_counts(50) == (1, 1)
    _fetch(monkeypatch, drive.Content(text=""))

    class _Emb:
        def embed_passages(self, xs):
            return [[0.1, 0.2, 0.3, 0.4] for _ in xs]
    ctx = ReflowContext(s, _Emb(), str(tmp_path), drive_service=_Svc(GDOC, NEW))
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is None
    assert s.owner_chunks(["gdrive-F-"]) == []
    assert s.reflow_live_counts(50) == (0, 0)


def test_reflow_routes_shared_drive_owner_through_the_cycle_handler(tmp_path, monkeypatch):
    """M4/Task 4: a shared-drive owner (chunks stamped drive_id) on a
    `reflow:drive` row is routed to the cycle's own "drive" handler as
    `drive:<id>`, which dispatches to handle_shared_drive_item; the emptied
    native file loses its chunks and the owner is no longer held."""
    from datetime import datetime, timezone
    s = _store(tmp_path)
    _seed(s, drive_id="D1")
    with s._connect(write=True) as db:
        db.execute("INSERT OR REPLACE INTO reflow_owners(owner, source, at, chunks_new,"
                   " carried, reenrich, outcome) VALUES('F','drive',?,0,0,0,'ordinary')",
                   (datetime.now(timezone.utc).isoformat(),))
    assert s.reflow_live_counts(50) == (1, 1)
    _fetch(monkeypatch, drive.Content(text=""))
    svc, fs, seen = _Svc(GDOC, NEW), LocalDirFleetStorage(tmp_path / "fs"), []

    def drive_handler(it):            # the shape sync/__init__ registers
        seen.append(it["source"])
        assert ":" in it["source"]
        return drive.handle_shared_drive_item(
            svc, s, it, fleet_storage=fs, pin=PIN,
            drive_id=it["source"].split(":", 1)[1])

    class _Emb:
        def embed_passages(self, xs):
            return [[0.1, 0.2, 0.3, 0.4] for _ in xs]
    ctx = ReflowContext(s, _Emb(), str(tmp_path), drive_service=svc,
                        normal_handlers={"drive": drive_handler})
    assert ctx.handle({"source": "reflow:drive", "ref_id": "F", "attempts": 0}) is None
    assert seen == ["drive:D1"]
    assert s.owner_chunks(["gdrive-F-"]) == []
    assert s.reflow_live_counts(50) == (0, 0)
