"""mcpbrain must never ingest its OWN infrastructure drives (backups, fleet
folder, escrow) as Shared Drive document sources."""
import pytest

from mcpbrain import config, infra_drives, ingest_cache
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage


@pytest.fixture(autouse=True)
def _no_tenant_profile(monkeypatch):
    # The build's tenant profile names real fleet/escrow folders; these tests
    # configure their own ids explicitly.
    from mcpbrain import tenant
    monkeypatch.setattr(tenant, "profile", lambda: None)
    infra_drives._warned.clear()


def _store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


class _Req:
    def __init__(self, result=None, exc=None):
        self._r, self._e = result, exc

    def execute(self, num_retries=0):
        if self._e:
            raise self._e
        return self._r


class _Files:
    def __init__(self, drive_of, fail=()):
        self.drive_of = drive_of        # file/folder id -> driveId (or None)
        self.fail = set(fail)
        self.calls = []

    def get(self, fileId, fields=None, supportsAllDrives=None):
        self.calls.append(fileId)
        if fileId in self.fail:
            return _Req(exc=OSError("network down"))
        did = self.drive_of.get(fileId)
        return _Req({"driveId": did} if did else {"id": fileId})


class _Svc:
    def __init__(self, drive_of=None, fail=()):
        self._files = _Files(drive_of or {}, fail)

    def files(self):
        return self._files


def _home(tmp_path, *, backup=None, fleet=None, escrow=None):
    home = str(tmp_path / "home")
    cfg = {"owner_email": "me@x.org"}
    if backup:
        cfg["backup"] = {"shared_drive_id": backup}
    if fleet or escrow:
        cfg["fleet"] = {"folder_id": fleet, "escrow_folder_id": escrow}
    config.write_config(home, cfg)
    return home


# -- resolution --------------------------------------------------------------

def test_listed_drive_id_is_excluded_without_a_drive_call(tmp_path):
    home = _home(tmp_path, backup="INFRA")
    svc = _Svc()
    got = infra_drives.resolve(home, _store(tmp_path), svc,
                               listed_drive_ids=["INFRA", "D1"])
    assert got == {"INFRA"}
    assert svc._files.calls == []


def test_folder_ids_resolve_to_their_drive_and_are_cached(tmp_path):
    home = _home(tmp_path, backup="BFOLDER", fleet="FLEET", escrow="ESCROW")
    s = _store(tmp_path)
    svc = _Svc({"BFOLDER": "INFRA", "FLEET": "INFRA", "ESCROW": "KEYS"})
    assert infra_drives.resolve(home, s, svc, listed_drive_ids=["INFRA", "KEYS", "D1"]) \
        == {"INFRA", "KEYS"}
    assert sorted(svc._files.calls) == ["BFOLDER", "ESCROW", "FLEET"]
    svc._files.calls.clear()
    assert infra_drives.resolve(home, s, svc, listed_drive_ids=["INFRA", "KEYS"]) \
        == {"INFRA", "KEYS"}
    assert svc._files.calls == []        # answered from the meta cache


def test_my_drive_folder_excludes_nothing_and_is_not_asked_again(tmp_path):
    home = _home(tmp_path, fleet="MYDRIVEFOLDER")
    s = _store(tmp_path)
    svc = _Svc({})
    assert infra_drives.resolve(home, s, svc, listed_drive_ids=["D1"]) == set()
    assert infra_drives.resolve(home, s, svc, listed_drive_ids=["D1"]) == set()
    assert svc._files.calls == ["MYDRIVEFOLDER"]


def test_failed_lookup_excludes_nothing_and_retries_next_cycle(tmp_path):
    home = _home(tmp_path, fleet="FLEET")
    s = _store(tmp_path)
    svc = _Svc({"FLEET": "INFRA"}, fail={"FLEET"})
    assert infra_drives.resolve(home, s, svc, listed_drive_ids=["INFRA"]) == set()
    svc._files.fail.clear()
    assert infra_drives.resolve(home, s, svc, listed_drive_ids=["INFRA"]) == {"INFRA"}
    assert svc._files.calls == ["FLEET", "FLEET"]


def test_nothing_configured_excludes_nothing(tmp_path):
    home = _home(tmp_path)
    assert infra_drives.resolve(home, _store(tmp_path), _Svc(),
                                listed_drive_ids=["D1"]) == set()


# -- discovery ----------------------------------------------------------------

def test_discover_shared_drives_never_lists_an_excluded_drive():
    from mcpbrain.sync import drive as drive_mod

    listed = []

    class _Changes:
        def getStartPageToken(self, driveId=None, supportsAllDrives=None):
            listed.append(driveId)
            return _Req({"startPageToken": "1"})

    class _Drives:
        def list(self, **_kw):
            return _Req({"drives": [{"id": "INFRA"}, {"id": "D1"}]})

    class _S:
        def changes(self):
            return _Changes()

        def drives(self):
            return _Drives()

    class _St:
        def get_cursor(self, _s):
            return None

        def set_cursor(self, *_a):
            pass

    out = drive_mod.discover_shared_drives(_S(), _St(), pin=None, exclude={"INFRA"})
    assert listed == ["D1"]
    assert set(out) == {"D1"}


# -- purge -------------------------------------------------------------------

def _seed_drive(s, drive_id, fid):
    s.import_cached_chunk(f"gdrive-{fid}-0", "text", "c",
                          {"file_id": fid, "drive_id": drive_id}, [0.1] * 4)


def test_purge_infrastructure_drive_uses_the_revocation_path(tmp_path):
    s = _store(tmp_path)
    _seed_drive(s, "INFRA", "F1")
    _seed_drive(s, "D1", "F2")
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('a','A','person','local')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('b','B','org','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,source_doc_id,origin) "
                   "VALUES('a','works_at','b','gdrive-F1-0','local')")
    s.set_cursor("drive:INFRA", "9")
    s.set_cursor("drive:INFRA_backfill_until", "2026-01-01T00:00:00")
    s.enqueue_and_advance([{"ref_id": "F1", "version": "1", "event": "upsert",
                            "modified_at": "2026-10-01T00:00:00"}],
                          source="drive:INFRA", cursor="9")
    s.record_pending_publish("INFRA", "F1", "h")
    s.set_meta(ingest_cache._KNOWN_DRIVES_META, '["D1", "INFRA"]')
    s.set_meta(ingest_cache._absence_key("INFRA"), "1")
    fs = LocalDirFleetStorage(tmp_path / "cache")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/F1__aaa__bbb.json.gz", b"x")

    res = ingest_cache.purge_infrastructure_drive(s, "INFRA", fleet_storage=fs)

    assert res["chunks_deleted"] == 1 and res["relations_invalidated"] == 1
    assert s.doc_ids_for_drive("INFRA") == []
    assert s.doc_ids_for_drive("D1") == ["gdrive-F2-0"]
    assert s.get_cursor("drive:INFRA") is None
    assert s.get_cursor("drive:INFRA_backfill_until") is None
    assert s.sync_queue_pending("drive:INFRA") == 0
    assert s.pending_publishes("INFRA") == []
    assert s.get_meta(ingest_cache._KNOWN_DRIVES_META) == '["D1"]'
    assert s.get_meta(ingest_cache._absence_key("INFRA")) is None
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == []
    with s._connect() as db:
        inv = db.execute("SELECT invalidated_at FROM entity_relations").fetchone()[0]
    assert inv is not None

    # Once purged it is not re-scanned every cycle.
    assert ingest_cache.purge_infrastructure_drive(s, "INFRA", fleet_storage=fs) is None


def test_purge_retries_cache_clear_after_a_transient_failure(tmp_path):
    s = _store(tmp_path)

    class _Flaky(LocalDirFleetStorage):
        fail = True

        def list_paths(self, prefix):
            if self.fail:
                raise OSError("transient")
            return super().list_paths(prefix)

    fs = _Flaky(tmp_path / "cache")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/F1__aaa__bbb.json.gz", b"x")
    ingest_cache.purge_infrastructure_drive(s, "INFRA", fleet_storage=fs)
    assert fs.get_bytes(f"{ingest_cache.CACHE_DIR}/F1__aaa__bbb.json.gz") == b"x"
    fs.fail = False
    ingest_cache.purge_infrastructure_drive(s, "INFRA", fleet_storage=fs)
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == []


# -- the sync cycle -------------------------------------------------------------

def test_run_sync_cycle_excludes_and_purges_the_backup_drive(tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    from mcpbrain.sync import run_sync_cycle
    from tests.test_drive_sync import FakeDriveService

    home = str(tmp_path / "home")
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}},
        "owner_email": "me@x.org",
        # A wizard install: the escrow FOLDER id, not the drive id.
        "backup": {"shared_drive_id": "BFOLDER"}})
    s = _store(tmp_path)
    _seed_drive(s, "INFRA", "F1")
    _seed_drive(s, "D1", "F2")
    s.set_cursor("drive:INFRA", "9")
    s.set_meta(ingest_cache._KNOWN_DRIVES_META, '["D1", "INFRA"]')

    svc = FakeDriveService(
        shared_drives=[{"id": "INFRA", "name": "Backups"}, {"id": "D1", "name": "Ops"}],
        files_by_id={"BFOLDER": {"id": "BFOLDER", "driveId": "INFRA"}})
    seen = {}

    def _spy_discover(service, store, *, pin, budget=None, exclude=()):
        seen["exclude"] = set(exclude)
        return {"D1": 0}

    def _spy_presence(store_, present, *, threshold):
        seen["present"] = sorted(present)
        return {"purged": [], "tracked": len(present)}

    def _spy_backfill(store_, drive_service, pin, drives, **_kw):
        seen["backfill"] = sorted(drives)
        return {}

    monkeypatch.setattr(sync_mod, "discover_shared_drives", _spy_discover)
    monkeypatch.setattr(sync_mod, "_shared_drive_backfill_step", _spy_backfill)
    monkeypatch.setattr(ingest_cache, "note_drive_presence", _spy_presence)
    monkeypatch.setattr(config, "ingest_cache_central", lambda home: False)
    import mcpbrain.fleet_storage as fsmod
    monkeypatch.setattr(fsmod, "drive_cache_storage",
                        lambda svc_, did: LocalDirFleetStorage(tmp_path / "c" / did))

    run_sync_cycle(s, embedder=None, drive_service=svc, home=home)

    assert seen["exclude"] == {"INFRA"}
    assert seen["present"] == ["D1"]
    assert seen["backfill"] == ["D1"]
    assert s.doc_ids_for_drive("INFRA") == []
    assert s.doc_ids_for_drive("D1") == ["gdrive-F2-0"]
    assert s.get_cursor("drive:INFRA") is None


def test_run_sync_cycle_leaves_drives_alone_when_no_infra_configured(tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    from mcpbrain.sync import run_sync_cycle
    from tests.test_drive_sync import FakeDriveService

    home = str(tmp_path / "home")
    config.write_config(home, {"org_config": {"org_pin": {
        "embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
        "enrich_logic_floor": 1, "fleet_secret": "s3cret"}},
        "owner_email": "me@x.org"})
    s = _store(tmp_path)
    _seed_drive(s, "D1", "F2")
    svc = FakeDriveService(shared_drives=[{"id": "D1", "name": "Ops"}])
    seen = {}

    def _spy_discover(*_a, exclude=(), **_k):
        seen["exclude"] = set(exclude)
        return {}

    monkeypatch.setattr(sync_mod, "discover_shared_drives", _spy_discover)
    monkeypatch.setattr(sync_mod, "_shared_drive_backfill_step", lambda *a, **k: {})

    run_sync_cycle(s, embedder=None, drive_service=svc, home=home)

    assert seen["exclude"] == set()
    assert s.doc_ids_for_drive("D1") == ["gdrive-F2-0"]


# -- onboarding ---------------------------------------------------------------

def test_onboarding_bootstrap_skips_infrastructure_drives(tmp_path):
    from mcpbrain import onboarding

    home = _home(tmp_path, backup="INFRA")
    s = _store(tmp_path)
    seen = []

    def _bootstrap_drive(store, fs, drive_id, pin):
        seen.append(drive_id)
        return {"status": "ok"}

    onboarding.run_bootstrap(
        home, s, fleet_storage=LocalDirFleetStorage(tmp_path / "fleet"),
        pin=config.fleet_pin(home).__class__(
            embed_model="bge-small", dim=4, chunker_version="v1",
            enrich_logic_floor=1, fleet_secret="s"),
        force=True, drives=["INFRA", "D1"],
        import_snapshot=lambda *a, **k: {"status": "imported"},
        bootstrap_drive=_bootstrap_drive)
    assert seen == ["D1"]
