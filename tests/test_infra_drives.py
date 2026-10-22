"""mcpbrain must never ingest its OWN infrastructure (backups, fleet folder,
escrow) -- and must never take an ordinary team drive down with it."""
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from mcpbrain import config, infra_drives, ingest_cache
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

FOLDER = "application/vnd.google-apps.folder"
PIN = {"embed_model": "bge-small", "dim": 4, "chunker_version": "v1",
       "enrich_logic_floor": 1, "fleet_secret": "s3cret"}


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


# -- a fake Drive tree -------------------------------------------------------

class _Req:
    def __init__(self, result=None, exc=None):
        self._r, self._e = result, exc

    def execute(self, num_retries=0):
        if self._e:
            raise self._e
        return self._r


class _Tree:
    """id -> {name, mimeType, parents, driveId}. Drive roots are named
    "Drive" with no parents, as Drive's files.get reports them."""

    def __init__(self, drives, items, fail=()):
        self.drives_ = drives                      # [{"id","name"}]
        self.nodes = {d["id"]: {"name": "Drive", "mimeType": FOLDER,
                                "parents": [], "driveId": d["id"]} for d in drives}
        self.nodes.update(items)
        self.fail = set(fail)
        self.calls = []

    # files()
    def files(self):
        return self

    def drives(self):
        return self

    def get(self, fileId, fields=None, supportsAllDrives=None):
        self.calls.append(("get", fileId))
        if fileId in self.fail:
            return _Req(exc=OSError("network down"))
        n = self.nodes.get(fileId)
        if n is None:
            return _Req({"id": fileId})
        return _Req({"id": fileId, **n})

    def list(self, q=None, pageToken=None, **_kw):
        if q is None:                               # drives().list
            return _Req({"drives": self.drives_})
        parent = re.match(r"'([^']+)' in parents", q).group(1)
        self.calls.append(("list", parent))
        kids = [{"id": i, "name": n["name"], "mimeType": n["mimeType"]}
                for i, n in self.nodes.items() if parent in n["parents"]]
        return _Req({"files": kids})


def _folder(name, parent, drive):
    return {"name": name, "mimeType": FOLDER, "parents": [parent], "driveId": drive}


def _file(name, parent, drive, mime="application/json"):
    return {"name": name, "mimeType": mime, "parents": [parent], "driveId": drive}


def _dedicated_tree(**kw):
    """INFRA holds only mcpbrain: mcpbrain-fleet (with escrow inside) and this
    person's backup folder. D1 is an ordinary team drive."""
    return _Tree([{"id": "INFRA", "name": "Backups"}, {"id": "D1", "name": "Ops"}], {
        "FLEET": _folder("mcpbrain-fleet", "INFRA", "INFRA"),
        "ESC": _folder("escrow", "FLEET", "INFRA"),
        "ME": _folder("me@x.org", "INFRA", "INFRA"),
        "CH": _folder("chain-20261001-abc", "ME", "INFRA"),
        "OTHER": _folder("someone@x.org", "INFRA", "INFRA"),
        "OCH": _folder("chain-20261002-def", "OTHER", "INFRA"),
        "DOC": _file("Plan.docx", "D1", "D1", "application/msword"),
    }, **kw)


def _team_tree():
    """A fork that put its fleet folder inside its ordinary "Ops" drive."""
    return _Tree([{"id": "TEAM", "name": "Ops"}], {
        "FLEET": _folder("mcpbrain-fleet", "TEAM", "TEAM"),
        "ESC": _folder("escrow", "FLEET", "TEAM"),
        "BUD": _folder("Budgets", "TEAM", "TEAM"),
        "PLAN": _file("Plan.docx", "BUD", "TEAM", "application/msword"),
        "CFG": _file("org-config.json", "FLEET", "TEAM"),
    })


def _home(tmp_path, *, backup=None, fleet=None, escrow=None, extra=None):
    home = str(tmp_path / "home")
    cfg = {"owner_email": "me@x.org", **(extra or {})}
    if backup:
        cfg["backup"] = {"shared_drive_id": backup}
    if fleet or escrow:
        cfg["fleet"] = {"folder_id": fleet, "escrow_folder_id": escrow}
    config.write_config(home, cfg)
    return home


# -- resolution --------------------------------------------------------------

def test_a_drive_holding_only_mcpbrain_is_dedicated(tmp_path):
    home = _home(tmp_path, backup="INFRA", fleet="FLEET", escrow="ESC")
    svc = _dedicated_tree()
    lay = infra_drives.resolve(home, _store(tmp_path), svc,
                               listed_drive_ids=["INFRA", "D1"])
    assert lay.dedicated == {"INFRA"}
    assert lay.folders["INFRA"] == {"FLEET", "ESC", "ME", "OTHER"}
    assert "D1" not in lay.folders
    assert "Drive/mcpbrain-fleet" in lay.paths["INFRA"]
    assert lay.in_infra("INFRA", "Drive/mcpbrain-fleet/org-graph")
    assert not lay.in_infra("INFRA", "Drive/mcpbrain-fleet-notes")


def test_a_team_drive_holding_the_fleet_folder_is_not_dedicated(tmp_path):
    home = _home(tmp_path, fleet="FLEET", escrow="ESC")
    lay = infra_drives.resolve(home, _store(tmp_path), _team_tree(),
                               listed_drive_ids=["TEAM"])
    assert lay.dedicated == frozenset()
    assert lay.folders["TEAM"] == {"FLEET", "ESC"}
    assert lay.paths["TEAM"] == ("Drive/mcpbrain-fleet", "Drive/mcpbrain-fleet/escrow")


def test_an_email_folder_holding_documents_is_not_a_backup_folder(tmp_path):
    home = _home(tmp_path, backup="INFRA", fleet="FLEET", escrow="ESC")
    svc = _dedicated_tree()
    svc.nodes["NOTES"] = _file("notes.txt", "OTHER", "INFRA", "text/plain")
    lay = infra_drives.resolve(home, _store(tmp_path), svc,
                               listed_drive_ids=["INFRA", "D1"])
    assert lay.dedicated == frozenset()
    assert "OTHER" not in lay.folders["INFRA"]


def test_a_backup_folder_id_config_resolves_to_its_drive(tmp_path):
    home = _home(tmp_path, backup="FLEET")       # wizard installs wrote a folder id
    lay = infra_drives.resolve(home, _store(tmp_path), _dedicated_tree(),
                               listed_drive_ids=["INFRA", "D1"])
    assert "FLEET" in lay.folders["INFRA"]


def test_the_layout_is_cached_and_reverified_after_seven_days(tmp_path):
    home = _home(tmp_path, backup="INFRA", fleet="FLEET", escrow="ESC")
    s = _store(tmp_path)
    svc = _dedicated_tree()
    t0 = datetime(2026, 10, 22, tzinfo=timezone.utc)
    infra_drives.resolve(home, s, svc, listed_drive_ids=["INFRA", "D1"], now=t0)
    svc.calls.clear()
    infra_drives.resolve(home, s, svc, listed_drive_ids=["INFRA", "D1"],
                         now=t0 + timedelta(days=6))
    assert svc.calls == []
    lay = infra_drives.resolve(home, s, svc, listed_drive_ids=["INFRA", "D1"],
                               now=t0 + timedelta(days=8))
    assert svc.calls
    assert lay.checked_at == (t0 + timedelta(days=8)).isoformat()


def test_a_cached_drive_missing_from_the_listing_is_reverified_at_once(tmp_path):
    home = _home(tmp_path, fleet="FLEET", escrow="ESC")
    s = _store(tmp_path)
    svc = _dedicated_tree()
    t0 = datetime(2026, 10, 22, tzinfo=timezone.utc)
    infra_drives.resolve(home, s, svc, listed_drive_ids=["INFRA", "D1"], now=t0)
    svc.calls.clear()
    infra_drives.resolve(home, s, svc, listed_drive_ids=["D1"],
                         now=t0 + timedelta(hours=1))
    assert ("get", "FLEET") in svc.calls


def test_failed_lookup_excludes_nothing_and_retries_next_cycle(tmp_path):
    home = _home(tmp_path, fleet="FLEET")
    s = _store(tmp_path)
    svc = _dedicated_tree(fail={"FLEET"})
    assert infra_drives.resolve(home, s, svc, listed_drive_ids=["INFRA"]).folders == {}
    svc.fail.clear()
    assert "FLEET" in infra_drives.resolve(home, s, svc,
                                           listed_drive_ids=["INFRA"]).folders["INFRA"]


def test_nothing_configured_excludes_nothing(tmp_path):
    home = _home(tmp_path)
    lay = infra_drives.resolve(home, _store(tmp_path), _dedicated_tree(),
                               listed_drive_ids=["INFRA"])
    assert lay.dedicated == frozenset() and lay.folders == {}


# -- discovery and ingest ------------------------------------------------------

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


def test_a_file_under_an_infra_folder_is_never_ingested(tmp_path, monkeypatch):
    from mcpbrain.sync import drive as drive_mod

    svc = _team_tree()
    tried = []
    monkeypatch.setattr(ingest_cache, "try_import",
                        lambda *a, **k: tried.append(a[3]) or True)
    item = {"ref_id": "CFG", "event": "upsert"}
    drive_mod.handle_shared_drive_item(
        svc, _store(tmp_path), item, fleet_storage=None, pin=None,
        drive_id="TEAM", skip_folders=frozenset({"FLEET", "ESC"}))
    assert tried == []
    drive_mod.handle_shared_drive_item(
        svc, _store(tmp_path), {"ref_id": "PLAN", "event": "upsert"},
        fleet_storage=None, pin=None, drive_id="TEAM",
        skip_folders=frozenset({"FLEET", "ESC"}))
    assert tried == ["PLAN"]


# -- purge -------------------------------------------------------------------

def _seed(s, drive_id, fid, *, folder, mime="application/json", n=1):
    for i in range(n):
        s.import_cached_chunk(f"gdrive-{fid}-{i}", "text", "c",
                              {"file_id": fid, "drive_id": drive_id,
                               "folder_path": folder, "mime_type": mime},
                              [0.1] * 4)


def _relation(s, doc_id, relation="works_at"):
    with s._connect() as db:
        db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                   "VALUES('a','A','person','local')")
        db.execute("INSERT OR IGNORE INTO entities(id,name,type,origin) "
                   "VALUES('b','B','org','local')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,"
                   "source_doc_id,origin) VALUES('a',?,'b',?,'local')",
                   (relation, doc_id))


def _invalidated(s, doc_id):
    with s._connect() as db:
        return db.execute("SELECT invalidated_at FROM entity_relations "
                          "WHERE source_doc_id=?", (doc_id,)).fetchone()[0]


def _layout(dedicated, folders, paths, checked="2026-10-22T00:00:00+00:00"):
    return infra_drives.Layout(dedicated=frozenset(dedicated),
                               folders={d: frozenset(v) for d, v in folders.items()},
                               paths={d: tuple(v) for d, v in paths.items()},
                               checked_at=checked)


DED = _layout({"INFRA"}, {"INFRA": {"FLEET"}}, {"INFRA": ["Drive/mcpbrain-fleet"]})


def test_dedicated_purge_uses_the_revocation_path(tmp_path):
    s = _store(tmp_path)
    _seed(s, "INFRA", "F1", folder="Drive/mcpbrain-fleet")
    _seed(s, "D1", "F2", folder="Drive/Budgets")
    _relation(s, "gdrive-F1-0")
    s.set_cursor("drive:INFRA", "9")
    s.set_cursor("drive:INFRA_backfill_until", "2026-01-01T00:00:00")
    s.enqueue_and_advance([{"ref_id": "F1", "version": "1", "event": "upsert",
                            "modified_at": "2026-10-01T00:00:00"}],
                          source="drive:INFRA", cursor="9")
    s.record_pending_publish("INFRA", "F1", "h")
    s.set_meta(ingest_cache._KNOWN_DRIVES_META, '["D1", "INFRA"]')
    s.set_meta(ingest_cache._absence_key("INFRA"), "1")
    fs = LocalDirFleetStorage(tmp_path / "cache")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/F1.aaaaaaaaaaaa.bbbbbbbb.mbc.gz", b"x")

    res = ingest_cache.purge_infrastructure(s, DED, "INFRA", fleet_storage=fs)

    assert res == {"chunks_deleted": 1, "relations_invalidated": 1, "refused": 0}
    assert s.doc_ids_for_drive("INFRA") == []
    assert s.doc_ids_for_drive("D1") == ["gdrive-F2-0"]
    assert _invalidated(s, "gdrive-F1-0") is not None
    assert s.get_cursor("drive:INFRA") is None
    assert s.get_cursor("drive:INFRA_backfill_until") is None
    assert s.sync_queue_pending("drive:INFRA") == 0
    assert s.pending_publishes("INFRA") == []
    assert s.get_meta(ingest_cache._KNOWN_DRIVES_META) == '["D1"]'
    assert s.get_meta(ingest_cache._absence_key("INFRA")) is None
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == []
    # Not re-scanned until the layout is checked again.
    _seed(s, "INFRA", "F3", folder="Drive/mcpbrain-fleet")
    ingest_cache.purge_infrastructure(s, DED, "INFRA", fleet_storage=fs)
    assert s.doc_ids_for_drive("INFRA") == ["gdrive-F3-0"]


@pytest.mark.parametrize("seed", [
    dict(fid="P1", folder="Drive/mcpbrain-fleet", mime="application/pdf"),
    dict(fid="P2", folder="Drive/Board papers"),
    dict(fid="P3", folder="Drive/mcpbrain-fleet", n=51),
])
def test_dedicated_purge_refuses_what_looks_like_real_content(tmp_path, seed):
    from mcpbrain import doctor

    s = _store(tmp_path)
    _seed(s, "INFRA", seed.pop("fid"), **seed)
    s.set_cursor("drive:INFRA", "9")
    s.set_meta(ingest_cache._KNOWN_DRIVES_META, '["INFRA"]')
    fs = LocalDirFleetStorage(tmp_path / "cache")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/F1.aaaaaaaaaaaa.bbbbbbbb.mbc.gz", b"x")

    before = len(s.doc_ids_for_drive("INFRA"))
    res = ingest_cache.purge_infrastructure(s, DED, "INFRA", fleet_storage=fs)

    assert res["refused"] == before and res["chunks_deleted"] == 0
    assert len(s.doc_ids_for_drive("INFRA")) == before
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") != []
    # Still excluded from sync and forgotten by the presence tracker, so the
    # absence purge (which has no such guard) can never reach it.
    assert s.get_cursor("drive:INFRA") is None
    assert s.get_meta(ingest_cache._KNOWN_DRIVES_META) == "[]"
    lines = doctor.infra_refused_lines(s)
    assert len(lines) == 1 and f"did not remove {before} item(s) from INFRA" in lines[0]
    assert "look like real documents" in lines[0]


def test_non_dedicated_purge_removes_only_the_infra_subtree(tmp_path):
    s = _store(tmp_path)
    lay = _layout((), {"TEAM": {"FLEET"}}, {"TEAM": ["Drive/mcpbrain-fleet"]})
    svc = _team_tree()
    svc.nodes["OG"] = _folder("org-graph", "FLEET", "TEAM")
    svc.nodes["GR"] = _file("manifest.json", "OG", "TEAM")
    _seed(s, "TEAM", "CFG", folder="Drive/mcpbrain-fleet")
    _seed(s, "TEAM", "GR", folder="Drive/mcpbrain-fleet/org-graph")
    _seed(s, "TEAM", "PLAN", folder="Drive/Budgets", mime="application/msword")
    _relation(s, "gdrive-CFG-0")
    _relation(s, "gdrive-PLAN-0", "member_of")
    s.set_cursor("drive:TEAM", "9")
    s.set_meta(ingest_cache._KNOWN_DRIVES_META, '["TEAM"]')
    s.record_pending_publish("TEAM", "CFG", "h")
    s.record_pending_publish("TEAM", "PLAN", "h2")
    fs = LocalDirFleetStorage(tmp_path / "cache")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/CFG.aaaaaaaaaaaa.bbbbbbbb.mbc.gz", b"x")
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/PLAN.aaaaaaaaaaaa.bbbbbbbb.mbc.gz", b"y")

    res = ingest_cache.purge_infrastructure(s, lay, "TEAM", fleet_storage=fs,
                                            drive_service=svc)

    assert res["chunks_deleted"] == 2 and res["refused"] == 0
    assert s.doc_ids_for_drive("TEAM") == ["gdrive-PLAN-0"]
    assert _invalidated(s, "gdrive-CFG-0") is not None
    assert _invalidated(s, "gdrive-PLAN-0") is None
    assert s.get_cursor("drive:TEAM") == "9"                 # still synced
    assert s.get_meta(ingest_cache._KNOWN_DRIVES_META) == '["TEAM"]'
    assert dict(s.pending_publishes("TEAM")) == {"PLAN": "h2"}
    assert fs.list_paths(ingest_cache.CACHE_DIR + "/") == [
        f"{ingest_cache.CACHE_DIR}/PLAN.aaaaaaaaaaaa.bbbbbbbb.mbc.gz"]


def test_non_dedicated_purge_refuses_real_content_inside_the_subtree(tmp_path):
    s = _store(tmp_path)
    lay = _layout((), {"TEAM": {"FLEET"}}, {"TEAM": ["Drive/mcpbrain-fleet"]})
    _seed(s, "TEAM", "X", folder="Drive/mcpbrain-fleet", mime="application/pdf")
    svc = _team_tree()
    svc.nodes["X"] = _file("scan.pdf", "FLEET", "TEAM", "application/pdf")
    res = ingest_cache.purge_infrastructure(s, lay, "TEAM", drive_service=svc)
    assert res["refused"] == 1
    assert s.doc_ids_for_drive("TEAM") == ["gdrive-X-0"]


def test_non_dedicated_purge_goes_by_folder_id_not_name(tmp_path):
    """Two root folders both named mcpbrain-fleet: only the configured one's
    files go; the namesake's JSON stays, and so does a file whose ancestry
    cannot be resolved."""
    s = _store(tmp_path)
    lay = _layout((), {"TEAM": {"FLEET"}}, {"TEAM": ["Drive/mcpbrain-fleet"]})
    svc = _team_tree()
    svc.nodes["TWIN"] = _folder("mcpbrain-fleet", "TEAM", "TEAM")
    svc.nodes["MINE"] = _file("settings.json", "TWIN", "TEAM")
    svc.nodes["LOST"] = _file("lost.json", "FLEET", "TEAM")
    svc.fail.add("LOST")
    for fid in ("CFG", "MINE", "LOST"):
        _seed(s, "TEAM", fid, folder="Drive/mcpbrain-fleet")

    res = ingest_cache.purge_infrastructure(s, lay, "TEAM", drive_service=svc)

    assert res["chunks_deleted"] == 1
    assert sorted(s.doc_ids_for_drive("TEAM")) == ["gdrive-LOST-0", "gdrive-MINE-0"]


def test_non_dedicated_purge_without_a_drive_service_deletes_nothing(tmp_path):
    s = _store(tmp_path)
    lay = _layout((), {"TEAM": {"FLEET"}}, {"TEAM": ["Drive/mcpbrain-fleet"]})
    _seed(s, "TEAM", "CFG", folder="Drive/mcpbrain-fleet")
    assert ingest_cache.purge_infrastructure(s, lay, "TEAM")["chunks_deleted"] == 0
    assert s.doc_ids_for_drive("TEAM") == ["gdrive-CFG-0"]


def test_note_drive_presence_never_counts_an_excluded_drive(tmp_path):
    s = _store(tmp_path)
    s.set_meta(ingest_cache._KNOWN_DRIVES_META, '["D1", "INFRA"]')
    s.set_meta(ingest_cache._absence_key("INFRA"), "2")
    for _ in range(5):
        out = ingest_cache.note_drive_presence(s, ["D1"], threshold=3,
                                               exclude={"INFRA"})
        assert out["purged"] == []
    assert s.get_meta(ingest_cache._KNOWN_DRIVES_META) == '["D1"]'
    assert s.get_meta(ingest_cache._absence_key("INFRA")) is None


# -- the sync cycle -------------------------------------------------------------

def _cycle_home(tmp_path, **cfg):
    home = str(tmp_path / "home")
    config.write_config(home, {"org_config": {"org_pin": PIN},
                               "owner_email": "me@x.org", **cfg})
    return home


def test_cycle_with_central_cache_excludes_dedicated_drive_and_deletes_only_its_cache(
        tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    from mcpbrain import fleet_storage as fsmod
    from mcpbrain.sync import run_sync_cycle

    home = _cycle_home(tmp_path, backup={"shared_drive_id": "INFRA"},
                       fleet={"folder_id": "FLEET", "escrow_folder_id": "ESC"})
    s = _store(tmp_path)
    _seed(s, "INFRA", "F1", folder="Drive/mcpbrain-fleet")
    _seed(s, "D1", "F2", folder="Drive/Budgets", mime="application/msword")
    s.set_cursor("drive:INFRA", "9")
    s.set_meta(ingest_cache._KNOWN_DRIVES_META, '["D1", "INFRA"]')

    fleet_root = tmp_path / "fleet"
    for rel in ("ingest-cache/INFRA/.mcpbrain-cache/F1.aaaaaaaaaaaa.bbbbbbbb.mbc.gz",
                "ingest-cache/D1/.mcpbrain-cache/F2.aaaaaaaaaaaa.bbbbbbbb.mbc.gz",
                "escrow/me.key"):
        LocalDirFleetStorage(fleet_root).put_bytes(rel, b"x")
    monkeypatch.setattr(config, "ingest_cache_central", lambda home: True)
    monkeypatch.setattr(fsmod, "centralized_cache_storage",
                        lambda svc, ffid, did: LocalDirFleetStorage(
                            fleet_root / "ingest-cache" / did))
    seen = {}

    def _spy_discover(service, store, *, pin, budget=None, exclude=()):
        seen["exclude"] = set(exclude)
        return {}

    def _spy_presence(store_, present, *, threshold, exclude=()):
        seen["present"] = sorted(present)
        return {"purged": [], "tracked": len(present)}

    def _spy_backfill(store_, drive_service, pin, drives, **kw):
        seen["backfill"] = sorted(drives)
        return {}

    monkeypatch.setattr(sync_mod, "discover_shared_drives", _spy_discover)
    monkeypatch.setattr(sync_mod, "_shared_drive_backfill_step", _spy_backfill)
    monkeypatch.setattr(ingest_cache, "note_drive_presence", _spy_presence)

    run_sync_cycle(s, embedder=None, drive_service=_dedicated_tree(), home=home)

    assert seen["exclude"] == {"INFRA"}
    assert seen["present"] == ["D1"]
    assert seen["backfill"] == ["D1"]
    assert s.doc_ids_for_drive("INFRA") == []
    assert s.doc_ids_for_drive("D1") == ["gdrive-F2-0"]
    assert s.get_cursor("drive:INFRA") is None
    left = sorted(p.relative_to(fleet_root).as_posix()
                  for p in fleet_root.rglob("*") if p.is_file())
    assert left == ["escrow/me.key",
                    "ingest-cache/D1/.mcpbrain-cache/F2.aaaaaaaaaaaa.bbbbbbbb.mbc.gz"]


def test_cycle_keeps_syncing_a_team_drive_that_holds_the_fleet_folder(
        tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    from mcpbrain.sync import run_sync_cycle

    home = _cycle_home(tmp_path, fleet={"folder_id": "FLEET", "escrow_folder_id": "ESC"})
    s = _store(tmp_path)
    _seed(s, "TEAM", "CFG", folder="Drive/mcpbrain-fleet")
    _seed(s, "TEAM", "PLAN", folder="Drive/Budgets", mime="application/msword")
    s.set_cursor("drive:TEAM", "9")
    monkeypatch.setattr(config, "ingest_cache_central", lambda home: False)
    import mcpbrain.fleet_storage as fsmod
    monkeypatch.setattr(fsmod, "drive_cache_storage",
                        lambda svc_, did: LocalDirFleetStorage(tmp_path / "c" / did))
    seen = {}

    def _spy_discover(service, store, *, pin, budget=None, exclude=()):
        seen["exclude"] = set(exclude)
        return {}

    def _spy_backfill(store_, drive_service, pin, drives, *, skip_folders=None, **kw):
        seen["backfill"] = sorted(drives)
        seen["skip"] = skip_folders
        return {}

    monkeypatch.setattr(sync_mod, "discover_shared_drives", _spy_discover)
    monkeypatch.setattr(sync_mod, "_shared_drive_backfill_step", _spy_backfill)

    run_sync_cycle(s, embedder=None, drive_service=_team_tree(), home=home)

    assert seen["exclude"] == set()
    assert seen["backfill"] == ["TEAM"]
    assert seen["skip"]["TEAM"] == {"FLEET", "ESC"}
    assert s.doc_ids_for_drive("TEAM") == ["gdrive-PLAN-0"]
    assert s.get_cursor("drive:TEAM") == "9"
    assert json.loads(s.get_meta(ingest_cache._KNOWN_DRIVES_META)) == ["TEAM"]


def test_a_failing_infra_purge_never_reaches_the_unguarded_revocation_purge(
        tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    from mcpbrain.sync import run_sync_cycle

    home = _cycle_home(tmp_path, backup={"shared_drive_id": "INFRA"},
                       fleet={"folder_id": "FLEET", "escrow_folder_id": "ESC"})
    s = _store(tmp_path)
    _seed(s, "INFRA", "F1", folder="Drive/mcpbrain-fleet", mime="application/pdf")
    s.set_meta(ingest_cache._KNOWN_DRIVES_META, '["D1", "INFRA"]')
    monkeypatch.setattr(config, "ingest_cache_central", lambda home: False)
    import mcpbrain.fleet_storage as fsmod
    monkeypatch.setattr(fsmod, "drive_cache_storage",
                        lambda svc_, did: LocalDirFleetStorage(tmp_path / "c" / did))
    monkeypatch.setattr(sync_mod, "discover_shared_drives", lambda *a, **k: {})
    monkeypatch.setattr(sync_mod, "_shared_drive_backfill_step", lambda *a, **k: {})

    def _boom(*_a, **_k):
        raise RuntimeError("purge keeps failing")

    purged = []
    real_purge_drive = ingest_cache.purge_drive
    monkeypatch.setattr(ingest_cache, "purge_infrastructure", _boom)
    monkeypatch.setattr(ingest_cache, "purge_drive",
                        lambda st, d: purged.append(d) or real_purge_drive(st, d))

    svc = _dedicated_tree()
    for _ in range(config.ingest_cache_revocation_threshold(home) + 2):
        run_sync_cycle(s, embedder=None, drive_service=svc, home=home)

    assert "INFRA" not in purged
    assert s.doc_ids_for_drive("INFRA") == ["gdrive-F1-0"]


def test_cycle_leaves_drives_alone_when_no_infra_configured(tmp_path, monkeypatch):
    from mcpbrain import sync as sync_mod
    from mcpbrain.sync import run_sync_cycle

    home = _cycle_home(tmp_path)
    s = _store(tmp_path)
    _seed(s, "D1", "F2", folder="Drive/Budgets")
    seen = {}

    def _spy_discover(*_a, exclude=(), **_k):
        seen["exclude"] = set(exclude)
        return {}

    monkeypatch.setattr(sync_mod, "discover_shared_drives", _spy_discover)
    monkeypatch.setattr(sync_mod, "_shared_drive_backfill_step", lambda *a, **k: {})

    run_sync_cycle(s, embedder=None, drive_service=_dedicated_tree(), home=home)

    assert seen["exclude"] == set()
    assert s.doc_ids_for_drive("D1") == ["gdrive-F2-0"]


# -- onboarding ---------------------------------------------------------------

def test_onboarding_bootstrap_skips_dedicated_drives_only(tmp_path):
    from mcpbrain import onboarding
    from mcpbrain.org_contracts import FleetPin

    home = _home(tmp_path, backup="INFRA", fleet="FLEET", escrow="ESC")
    seen = []

    def _bootstrap_drive(store, fs, drive_id, pin):
        seen.append(drive_id)
        return {"status": "ok"}

    onboarding.run_bootstrap(
        home, _store(tmp_path), drive_service=_dedicated_tree(),
        fleet_storage=LocalDirFleetStorage(tmp_path / "fleet"),
        pin=FleetPin(**PIN), force=True, drives=["INFRA", "D1"],
        make_drive_storage=lambda d: LocalDirFleetStorage(tmp_path / d),
        import_snapshot=lambda *a, **k: {"status": "imported"},
        bootstrap_drive=_bootstrap_drive)
    assert seen == ["D1"]
