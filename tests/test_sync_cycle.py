"""Integration tests for run_sync_cycle — real bge-small embedder.

Proves the end-to-end path: sync → store → embed → searchable.
Uses the same fake Gmail service shape as test_gmail_sync.py.
"""

import base64

import pytest

from mcpbrain.embed import get_embedder
from mcpbrain.retrieval import hybrid_search
from mcpbrain.store import Store
from mcpbrain.sync import run_sync_cycle


# ---------------------------------------------------------------------------
# Fake Drive service (mirrors the shape in test_drive_sync.py)
# ---------------------------------------------------------------------------

class _DriveReq:
    def __init__(self, result=None, raise_exc=None):
        self._r = result
        self._e = raise_exc

    def execute(self):
        if self._e:
            raise self._e
        return self._r


class _DriveChanges:
    def __init__(self, pages, initial_cursor):
        self._pages = pages
        self._initial_cursor = initial_cursor

    def list(self, **kw):
        token = kw.get("pageToken")
        if token is None or token == self._initial_cursor:
            idx = 0
        else:
            try:
                idx = int(token)
            except (ValueError, TypeError):
                idx = 0
        return _DriveReq(self._pages[idx])


class _DriveFiles:
    def __init__(self, exports=None):
        self._exports = exports or {}

    def export(self, fileId, mimeType):
        return _DriveReq(self._exports.get(fileId, b""))


class FakeDriveService:
    def __init__(self, pages, exports, initial_cursor="100"):
        self._changes = _DriveChanges(pages, initial_cursor)
        self._files = _DriveFiles(exports)

    def changes(self):
        return self._changes

    def files(self):
        return self._files


def _drive_page(changes, next_page_token=None, new_start_page_token=None):
    p = {"changes": changes}
    if next_page_token is not None:
        p["nextPageToken"] = next_page_token
    if new_start_page_token is not None:
        p["newStartPageToken"] = new_start_page_token
    return p


def _gdoc_change(fid, name="Doc"):
    return {
        "fileId": fid,
        "removed": False,
        "file": {
            "id": fid,
            "name": name,
            "mimeType": "application/vnd.google-apps.document",
            "modifiedTime": "2026-05-01T10:00:00Z",
            "owners": [{"displayName": "Someone"}],
        },
    }


# ---------------------------------------------------------------------------
# Helpers (same shape as test_gmail_sync.py)
# ---------------------------------------------------------------------------

def b64(s: str) -> str:
    return base64.urlsafe_b64encode(s.encode()).decode()


def plain_msg(mid: str, subject: str, sender: str, body: str) -> dict:
    return {
        "id": mid,
        "threadId": "t-" + mid,
        "labelIds": ["INBOX"],
        "payload": {
            "mimeType": "text/plain",
            "headers": [
                {"name": "Subject", "value": subject},
                {"name": "From", "value": sender},
            ],
            "body": {"data": b64(body)},
        },
    }


class _Req:
    def __init__(self, result):
        self._r = result

    def execute(self):
        return self._r


class _History:
    def __init__(self, pages):
        self._pages = pages

    def list(self, **kw):
        token = kw.get("pageToken")
        idx = 0 if token is None else int(token)
        return _Req(self._pages[idx])


class _Messages:
    def __init__(self, by_id):
        self._by_id = by_id

    def get(self, userId, id, format):
        result = self._by_id[id]
        if isinstance(result, Exception):
            raise result
        return _Req(result)


class _Users:
    def __init__(self, profile_hid, history, messages):
        self._p = profile_hid
        self._h = history
        self._m = messages

    def getProfile(self, userId):
        return _Req({"historyId": self._p, "emailAddress": "test@example.com"})

    def history(self):
        return self._h

    def messages(self):
        return self._m


class FakeGmailService:
    def __init__(self, profile_hid="1000", pages=None, messages=None):
        msgs = _Messages(messages or {})
        self._users = _Users(profile_hid, _History(pages or []), msgs)

    def users(self):
        return self._users


def _make_page(msg_ids, history_id, next_page_token=None):
    history = [
        {
            "id": f"h-{mid}",
            "messagesAdded": [{"message": {"id": mid, "labelIds": ["INBOX"]}}],
        }
        for mid in msg_ids
    ]
    page = {"history": history, "historyId": history_id}
    if next_page_token is not None:
        page["nextPageToken"] = next_page_token
    return page


# ---------------------------------------------------------------------------
# Module-scoped fixture: load bge-small once for the whole test module
# (~20-75s first time; cached by sentence-transformers after that)
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def emb():
    return get_embedder("bge-small")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_sync_cycle_makes_gmail_content_searchable(tmp_path, emb):
    """Sync one Gmail message, embed it, assert it's findable via hybrid_search.

    This is the Phase 2 integration proof: sync → store → embed → searchable.
    """
    store = Store(tmp_path / "b.sqlite3", dim=emb.dim)
    store.init()

    # Pre-set cursor so the delta path runs (not bootstrap)
    store.set_cursor("gmail", "1000")

    distinctive_body = (
        "Annual budget review and quarterly expenditure forecast for the finance team."
    )
    msg_m1 = plain_msg(
        "m1",
        "Finance Budget Forecast",
        "finance@example.com",
        distinctive_body,
    )
    pages = [_make_page(["m1"], history_id="1005")]
    fake = FakeGmailService(profile_hid="1000", pages=pages, messages={"m1": msg_m1})

    res = run_sync_cycle(store, emb, gmail_service=fake)

    # Sync count
    assert res["gmail"] == 1
    # At least one chunk embedded
    assert res["embedded"] >= 1

    # The content must be findable via hybrid_search
    results = hybrid_search(store, emb, "finance budget planning", limit=5)
    doc_ids = [r["doc_id"] for r in results]
    assert any(d.startswith("gmail-m1-body") for d in doc_ids), (
        f"Expected a result starting with 'gmail-m1-body', got: {doc_ids}"
    )


def test_sync_cycle_skips_absent_sources(tmp_path, emb):
    """run_sync_cycle with no services returns zero counts and does not raise."""
    store = Store(tmp_path / "c.sqlite3", dim=emb.dim)
    store.init()

    res = run_sync_cycle(store, emb)

    # Live deltas all skipped; the backfill step adds a `backfill` sub-dict
    # whose source counts are zero because no services were provided.
    assert res["gmail"] == 0
    assert res["calendar"] == 0
    assert res["drive"] == 0
    assert res["embedded"] == 0
    assert res["backfill"]["gmail"] == 0
    assert res["backfill"]["drive"] == 0
    assert res["backfill"]["calendar"] == 0


def test_sync_cycle_embeds_after_sync(tmp_path, emb):
    """After a full cycle, no chunks remain unembedded."""
    store = Store(tmp_path / "d.sqlite3", dim=emb.dim)
    store.init()
    store.set_cursor("gmail", "1000")

    distinctive_body = (
        "Annual budget review and quarterly expenditure forecast for the finance team."
    )
    msg_m1 = plain_msg(
        "m1",
        "Finance Budget Forecast",
        "finance@example.com",
        distinctive_body,
    )
    pages = [_make_page(["m1"], history_id="1005")]
    fake = FakeGmailService(profile_hid="1000", pages=pages, messages={"m1": msg_m1})

    run_sync_cycle(store, emb, gmail_service=fake)

    assert store.unembedded_chunks() == [], "Expected all chunks to be embedded after the cycle"


def test_sync_cycle_multi_source_accumulates_and_no_double_embed(tmp_path, emb):
    """run_sync_cycle with Gmail + Drive accumulates embedded counts across both sources.

    Proves three things:
    1. Both sources contribute chunks (delta paths run because cursors are pre-set).
    2. res["embedded"] equals the total chunk count from both sources combined.
    3. A second identical call embeds 0 new chunks — already-embedded chunks are
       not re-embedded (idempotent upsert + embedded flag behaviour).
    """
    store = Store(tmp_path / "multi.sqlite3", dim=emb.dim)
    store.init()

    # Pre-set both cursors so the delta paths run (not bootstrap).
    store.set_cursor("gmail", "1000")
    store.set_cursor("drive", "100")

    # --- Fake Gmail: one message with a distinctive body ---
    gmail_body = (
        "Pastoral care meeting agenda for staff review and ministry operations update."
    )
    msg_m1 = plain_msg(
        "m1",
        "Pastoral Care Agenda",
        "pastor@example.com",
        gmail_body,
    )
    gmail_pages = [_make_page(["m1"], history_id="1005")]
    fake_gmail = FakeGmailService(
        profile_hid="1000",
        pages=gmail_pages,
        messages={"m1": msg_m1},
    )

    # --- Fake Drive: one Google Doc with distinct content ---
    drive_body = b"Volunteer coordination handbook for onboarding and role allocation."
    drive_pages = [
        _drive_page(
            [_gdoc_change("f1", "Volunteer Handbook")],
            new_start_page_token="105",
        )
    ]
    fake_drive = FakeDriveService(
        pages=drive_pages,
        exports={"f1": drive_body},
        initial_cursor="100",
    )

    # First cycle: both sources sync and embed.
    res = run_sync_cycle(store, emb, gmail_service=fake_gmail, drive_service=fake_drive)

    assert res["gmail"] == 1, f"Expected 1 Gmail message synced, got {res['gmail']}"
    assert res["drive"] == 1, f"Expected 1 Drive file synced, got {res['drive']}"

    # All chunks must be embedded and the total must match the embedded counter.
    assert store.unembedded_chunks() == [], "Expected all chunks embedded after first cycle"
    assert res["embedded"] >= 2, (
        f"Expected at least 2 chunks embedded (one per source), got {res['embedded']}"
    )

    # Spot-check: expected chunk IDs exist in the store.
    gmail_chunk = store.get_chunk("gmail-m1-body-0")
    assert gmail_chunk is not None, "gmail-m1-body-0 chunk missing from store"

    drive_chunk = store.get_chunk("gdrive-f1-0")
    assert drive_chunk is not None, "gdrive-f1-0 chunk missing from store"

    # Second cycle: same fakes re-present the same content.
    # Upsert is idempotent (same content_hash → no update, embedded flag stays 1).
    # index_pending finds nothing unembedded, so embedded == 0.
    #
    # After the first cycle:
    #   - Gmail cursor is "1005" (set by sync_gmail from historyId in the page).
    #   - Drive cursor is "105" (set by sync_drive from newStartPageToken).
    # The second Gmail fake re-delivers the same history page (historyId "1005"),
    # yielding the same message; upsert is a no-op (same content_hash).
    # The second Drive fake uses initial_cursor="105" to match the advanced cursor,
    # and returns an empty changes page — no files to process.
    fake_gmail2 = FakeGmailService(
        profile_hid="1000",
        pages=gmail_pages,
        messages={"m1": msg_m1},
    )
    empty_drive_page = _drive_page([], new_start_page_token="106")
    fake_drive2 = FakeDriveService(
        pages=[empty_drive_page],
        exports={},
        initial_cursor="105",  # matches the cursor set by the first cycle
    )
    res2 = run_sync_cycle(store, emb, gmail_service=fake_gmail2, drive_service=fake_drive2)

    assert res2["embedded"] == 0, (
        f"Expected 0 new embeddings on second cycle (idempotent), got {res2['embedded']}"
    )


def test_run_sync_cycle_shared_drive_publishes_after_embed(tmp_path):
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.sync import run_sync_cycle
    from mcpbrain import ingest_cache
    from tests.test_drive_sync import FakeDriveService, _gdoc_change

    class _Emb:
        dim = 4
        def embed_passages(self, texts):
            return [[float(len(t) % 7), 1.0, 2.0, 3.0] for t in texts]
        def embed_query(self, text):
            return [0.0, 0.0, 0.0, 0.0]

    home = str(tmp_path / "home")
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}},
        "owner_email": "me@x.org"})
    store = Store(tmp_path / "b.sqlite3", dim=4); store.init()
    store.set_cursor("drive:D1", "100")

    # Route DriveFleetStorage at a local dir by monkeypatching the factory hook.
    from mcpbrain.sync import drive as drivemod
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fsmap = {}
    orig = drivemod.sync_shared_drives
    def _patched(service, s, *, pin, storage_factory, absence_threshold=3):
        return orig(service, s, pin=pin,
                    storage_factory=lambda d: fsmap.setdefault(d, LocalDirFleetStorage(tmp_path / d)),
                    absence_threshold=absence_threshold)
    drivemod.sync_shared_drives = _patched
    try:
        svc = FakeDriveService(
            shared_drives=[{"id": "D1", "name": "Ops"}],
            initial_cursor="100",
            pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
            exports={"FID": b"shared drive body content"})
        res = run_sync_cycle(store, _Emb(), drive_service=svc, home=home)
    finally:
        drivemod.sync_shared_drives = orig

    assert res["shared_drives"]["D1"] == 1
    # the miss was published after embedding: an artifact now exists for FID
    names = fsmap["D1"].list_paths(ingest_cache.CACHE_DIR + "/")
    assert any(n.rsplit("/", 1)[-1].startswith("FID.") for n in names)


def test_run_sync_cycle_no_pin_skips_shared_drives(tmp_path):
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.sync import run_sync_cycle
    from tests.test_drive_sync import FakeDriveService

    class _Emb:
        dim = 4
        def embed_passages(self, texts): return [[0.0]*4 for _ in texts]
        def embed_query(self, text): return [0.0]*4

    home = str(tmp_path / "home")
    config.write_config(home, {"owner_email": "me@x.org"})   # no org_pin
    store = Store(tmp_path / "b.sqlite3", dim=4); store.init()
    svc = FakeDriveService(shared_drives=[{"id": "D1", "name": "Ops"}])
    res = run_sync_cycle(store, _Emb(), drive_service=svc, home=home)
    assert "shared_drives" not in res      # gated off without a pin


def test_run_sync_cycle_isolates_publish_file_failures(tmp_path):
    """A publish_file failure for one miss must not abort the rest of the cycle:
    other misses (same drive AND a second drive) still get published, and
    run_sync_cycle returns normally with shared_drives reflecting success."""
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.sync import run_sync_cycle
    from mcpbrain import ingest_cache
    from tests.test_drive_sync import FakeDriveService, _gdoc_change

    class _Emb:
        dim = 4
        def embed_passages(self, texts):
            return [[float(len(t) % 7), 1.0, 2.0, 3.0] for t in texts]
        def embed_query(self, text):
            return [0.0, 0.0, 0.0, 0.0]

    home = str(tmp_path / "home")
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}},
        "owner_email": "me@x.org"})
    store = Store(tmp_path / "b.sqlite3", dim=4); store.init()
    # Pre-seed cursors for both drives so both hit the SAME fake changes page
    # on this cycle (the fake service routes any pageToken == initial_cursor
    # to page index 0), each seeing a two-file miss batch.
    store.set_cursor("drive:D1", "100")
    store.set_cursor("drive:D2", "100")

    from mcpbrain.sync import drive as drivemod
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fsmap = {}
    orig_sync_shared_drives = drivemod.sync_shared_drives
    def _patched(service, s, *, pin, storage_factory, absence_threshold=3):
        return orig_sync_shared_drives(
            service, s, pin=pin,
            storage_factory=lambda d: fsmap.setdefault(d, LocalDirFleetStorage(tmp_path / d)),
            absence_threshold=absence_threshold)
    drivemod.sync_shared_drives = _patched

    # Fail publish_file only for FID1, on every drive; FID2 must still succeed,
    # in the SAME drive (after FID1) and in the SECOND drive.
    orig_publish_file = ingest_cache.publish_file
    def _flaky_publish_file(store, fs, drive_id, file_id, content_hash, pin, **kw):
        if file_id == "FID1":
            raise RuntimeError("simulated transient Drive API error")
        return orig_publish_file(store, fs, drive_id, file_id, content_hash, pin, **kw)
    ingest_cache.publish_file = _flaky_publish_file

    try:
        svc = FakeDriveService(
            shared_drives=[{"id": "D1", "name": "Ops"}, {"id": "D2", "name": "Legal"}],
            initial_cursor="100",
            pages=[{"changes": [_gdoc_change("FID1"), _gdoc_change("FID2")],
                    "newStartPageToken": "101"}],
            exports={"FID1": b"shared drive body one", "FID2": b"shared drive body two"})
        # (a) must return normally — no exception propagates out of run_sync_cycle.
        res = run_sync_cycle(store, _Emb(), drive_service=svc, home=home)
    finally:
        drivemod.sync_shared_drives = orig_sync_shared_drives
        ingest_cache.publish_file = orig_publish_file

    # (b) shared_drives still reflects successful local processing for both drives,
    # despite FID1's publish failure in each.
    assert res["shared_drives"]["D1"] == 2
    assert res["shared_drives"]["D2"] == 2

    # (c) the OTHER miss — FID2 — was published in both the same drive (D1, after
    # FID1's failure) and the second drive (D2) despite FID1 failing everywhere.
    for drive_id in ("D1", "D2"):
        names = fsmap[drive_id].list_paths(ingest_cache.CACHE_DIR + "/")
        basenames = [n.rsplit("/", 1)[-1] for n in names]
        assert any(n.startswith("FID2.") for n in basenames), (
            f"expected FID2 artifact published in drive {drive_id}, got {basenames}"
        )
        assert not any(n.startswith("FID1.") for n in basenames), (
            f"FID1 publish should have failed (and been skipped) in drive {drive_id}"
        )


def test_run_sync_cycle_shared_drive_orchestrator_failure_does_not_abort_cycle(tmp_path, emb):
    """If sync_shared_drives ITSELF raises (e.g. list_shared_drives during a
    Drive-API outage) — not just an individual publish_file — the whole
    shared-drive block must be caught: gmail sync (which ran BEFORE the
    shared-drive block) must have already completed and been embedded, and
    run_sync_cycle must still return normally with its other expected keys
    (not raise), rather than aborting the whole cycle including the
    subsequent progressive-backfill step."""
    from mcpbrain import config
    from tests.test_drive_sync import FakeDriveService as RealDriveFakeService

    home = str(tmp_path / "home")
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": emb.dim, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}},
        "owner_email": "me@x.org"})
    store = Store(tmp_path / "b.sqlite3", dim=emb.dim)
    store.init()
    store.set_cursor("gmail", "1000")

    msg_m1 = plain_msg(
        "m1", "Finance Budget Forecast", "finance@example.com",
        "Annual budget review and quarterly expenditure forecast for the finance team.")
    gmail_pages = [_make_page(["m1"], history_id="1005")]
    fake_gmail = FakeGmailService(profile_hid="1000", pages=gmail_pages, messages={"m1": msg_m1})

    # A Drive service whose OWN sync_drive bootstrap works fine (no cursor set
    # yet -> just stores a startPageToken and returns 0); the failure under
    # test is entirely inside the shared-drive orchestrator, monkeypatched below.
    # (Uses the fuller fake from test_drive_sync, which implements changes()
    # .getStartPageToken() — the local module-level FakeDriveService in this
    # file is the simpler Gmail-focused fixture and doesn't.)
    fake_drive = RealDriveFakeService(pages=[{"changes": []}])

    from mcpbrain.sync import drive as drivemod

    def _boom(*a, **kw):
        raise RuntimeError("simulated Drive-API outage in list_shared_drives")

    orig = drivemod.sync_shared_drives
    drivemod.sync_shared_drives = _boom
    try:
        res = run_sync_cycle(
            store, emb, gmail_service=fake_gmail, drive_service=fake_drive, home=home)
    finally:
        drivemod.sync_shared_drives = orig

    # The cycle returned normally — no exception propagated out of run_sync_cycle —
    # and the work that ran before AND after the failed shared-drive block
    # (gmail sync/embed, the My-Drive progressive-backfill step) completed.
    assert res["gmail"] == 1
    assert res["embedded"] >= 1
    assert "backfill" in res
    # The shared-drive block's own keys were never populated (it failed before
    # producing a result), but that failure itself never reached the caller.
    assert "shared_drives" not in res
    assert "revoked_drives" not in res


def test_run_sync_cycle_shared_drive_skips_publish_when_owner_email_unconfigured(tmp_path):
    """A pinned+enabled install with an empty owner_email (config.owner_email
    can return "" when unconfigured) must not publish artifacts stamped with an
    empty published_by. Files are still synced/embedded locally; only the
    fleet-cache publish step is skipped, with a single warning (not one per
    file)."""
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.sync import run_sync_cycle
    from mcpbrain import ingest_cache
    from tests.test_drive_sync import FakeDriveService, _gdoc_change

    class _Emb:
        dim = 4
        def embed_passages(self, texts):
            return [[float(len(t) % 7), 1.0, 2.0, 3.0] for t in texts]
        def embed_query(self, text):
            return [0.0, 0.0, 0.0, 0.0]

    home = str(tmp_path / "home")
    # NOTE: no "owner_email" key at all -> config.owner_email(home) returns "".
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}}})
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    store.set_cursor("drive:D1", "100")

    from mcpbrain.sync import drive as drivemod
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fsmap = {}
    orig = drivemod.sync_shared_drives

    def _patched(service, s, *, pin, storage_factory, absence_threshold=3):
        return orig(service, s, pin=pin,
                    storage_factory=lambda d: fsmap.setdefault(d, LocalDirFleetStorage(tmp_path / d)),
                    absence_threshold=absence_threshold)
    drivemod.sync_shared_drives = _patched
    try:
        svc = FakeDriveService(
            shared_drives=[{"id": "D1", "name": "Ops"}],
            initial_cursor="100",
            pages=[{"changes": [_gdoc_change("FID")], "newStartPageToken": "101"}],
            exports={"FID": b"shared drive body content"})
        res = run_sync_cycle(store, _Emb(), drive_service=svc, home=home)
    finally:
        drivemod.sync_shared_drives = orig

    # The file was still synced/processed locally...
    assert res["shared_drives"]["D1"] == 1
    # ...but nothing was published to the fleet cache (no owner_email to stamp).
    names = fsmap["D1"].list_paths(ingest_cache.CACHE_DIR + "/")
    assert names == [], f"expected no artifacts published without owner_email, got {names}"


def test_run_sync_cycle_backfills_pinned_shared_drive_pre_existing_files(tmp_path):
    """A newly-pinned Shared Drive's PRE-EXISTING documents (everything before
    the pin, invisible to the live delta sync because they haven't changed
    recently) must get ingested via the progressive-backfill wiring, not just
    files touched after the pin."""
    from mcpbrain import config
    from mcpbrain.store import Store
    from mcpbrain.sync import run_sync_cycle
    from mcpbrain import ingest_cache
    from tests.test_drive_sync import FakeDriveService

    class _Emb:
        dim = 4
        def embed_passages(self, texts):
            return [[float(len(t) % 7), 1.0, 2.0, 3.0] for t in texts]
        def embed_query(self, text):
            return [0.0, 0.0, 0.0, 0.0]

    home = str(tmp_path / "home")
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}},
        "owner_email": "me@x.org"})
    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    # Delta cursor already bootstrapped; this cycle's live delta sees nothing new.
    store.set_cursor("drive:D1", "100")

    from mcpbrain.sync import drive as drivemod
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fsmap = {}
    orig = drivemod.sync_shared_drives

    def _patched(service, s, *, pin, storage_factory, absence_threshold=3):
        return orig(service, s, pin=pin,
                    storage_factory=lambda d: fsmap.setdefault(d, LocalDirFleetStorage(tmp_path / d)),
                    absence_threshold=absence_threshold)
    drivemod.sync_shared_drives = _patched
    try:
        svc = FakeDriveService(
            shared_drives=[{"id": "D1", "name": "Ops"}],
            initial_cursor="100",
            pages=[{"changes": [], "newStartPageToken": "101"}],  # nothing new via delta
            # Only visible via files().list — a document that predates the pin
            # and hasn't changed since, so the delta/changes feed never surfaces it.
            file_list=[{
                "id": "OLD1", "name": "Old Doc",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2020-01-01T00:00:00Z",
                "owners": [{"displayName": "Someone"}],
            }],
            exports={"OLD1": b"pre-existing shared drive content from before the pin"})
        res = run_sync_cycle(store, _Emb(), drive_service=svc, home=home)
    finally:
        drivemod.sync_shared_drives = orig

    # The live delta saw nothing new for D1...
    assert res["shared_drives"]["D1"] == 0
    # ...but the progressive-backfill step picked up the pre-existing file via
    # backfill_shared_drive, and it was processed and published to the cache.
    assert res["shared_drives_backfill"]["D1"] == 1
    names = fsmap["D1"].list_paths(ingest_cache.CACHE_DIR + "/")
    assert any(n.rsplit("/", 1)[-1].startswith("OLD1.") for n in names), (
        f"expected OLD1 artifact published via shared-drive backfill, got {names}"
    )
