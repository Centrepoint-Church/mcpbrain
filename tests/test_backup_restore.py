"""Restore (incremental backup, Task 7): newest across locations, resumable,
verified.

Chains are REAL: `backup_outbox.capture` on tiny SQLite stores, shipped by
`backup_uploader.upload_step` into the uploader tests' fake Drive. This file
adds folder listings and `get_media` downloads (served to the real
MediaIoBaseDownload through a fake http) over the same file store, so restore
reads exactly the manifests and segments the uploader wrote.
"""
import errno
import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import httplib2
import pytest

from mcpbrain import backup, backup_outbox as bo, backup_restore as br, config
from mcpbrain import backup_uploader as bu
from tests.test_backup_uploader import (KEY, Server, Service, _Exec, _insert,
                                        _make_db)
from tests.test_drive_resumable import FakeTransport

FOLDER_MIME = backup.FOLDER_MIME
OCT19 = datetime(2026, 10, 19, 9, 0, tzinfo=timezone.utc).timestamp()


def _iso(ts):
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")


class _Http:
    """Answers MediaIoBaseDownload's ranged GETs from the server's files."""

    def __init__(self, server, fid):
        self.server, self.fid = server, fid

    def request(self, uri, method="GET", headers=None, **kw):
        data = self.server.files.get(self.fid)
        if data is None:
            return httplib2.Response({"status": 404}), b'{"error": {"code": 404}}'
        m = re.match(r"bytes=(\d+)-(\d+)", (headers or {}).get("range", ""))
        first, last = (int(m.group(1)), int(m.group(2))) if m else (0, len(data) - 1)
        body = data[first:last + 1]
        self.server.downloads.append(self.fid)
        for hook in list(self.server.on_download):
            hook(self.fid)
        return httplib2.Response({
            "status": 206,
            "content-range": f"bytes {first}-{first + len(body) - 1}/{len(data)}",
        }), body


class _Req:
    def __init__(self, server, fid):
        self.uri = f"https://drive.example/media/{fid}"
        self.headers = {}
        self.http = _Http(server, fid)


class RService(Service):
    """Service with parent listings (name =/contains, mimeType =/!=) and
    get_media. Files added via `meta` may carry createdTime."""

    def files(self):
        base = super().files()
        outer = self

        class F:
            get, create, update = base.get, base.create, base.update

            def list(self, q, **kw):
                assert kw.get("supportsAllDrives") is True
                parent = re.search(r"'([^']*)' in parents", q).group(1)
                hits = []
                for fid, m in outer._all().items():
                    if parent not in m.get("parents", []):
                        continue
                    mime = m.get("mimeType", "application/octet-stream")
                    if (eq := re.search(r"name = '([^']*)'", q)) and m["name"] != eq.group(1):
                        continue
                    if (c := re.search(r"name contains '([^']*)'", q)) and c.group(1) not in m["name"]:
                        continue
                    if f"mimeType = '{FOLDER_MIME}'" in q and mime != FOLDER_MIME:
                        continue
                    if f"mimeType != '{FOLDER_MIME}'" in q and mime == FOLDER_MIME:
                        continue
                    hits.append({"id": fid, "name": m["name"], "mimeType": mime,
                                 "createdTime": m.get("createdTime", "")})
                outer.server.lists.append(q)
                resp = {"files": hits}
                if parent in outer.server.incomplete:
                    resp["incompleteSearch"] = True
                return _Exec(lambda: resp)

            def get_media(self, fileId, supportsAllDrives=None, **kw):
                assert supportsAllDrives is True
                return _Req(outer.server, fileId)
        return F()


@pytest.fixture
def env(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("MCPBRAIN_HOME", str(home))
    config.write_config(str(home), {
        "owner_email": "Owner@Example.org",
        "backup": {"shared_drive_id": "0Adrive", "user_id": "owner"},
        "fleet": {"escrow_folder_id": "ESC"},
    })
    store = _make_db(tmp_path / "brain.sqlite3")
    records = tmp_path / "records"
    records.mkdir()
    (records / "a.md").write_text("v1")
    cfg = tmp_path / "config.json"
    cfg.write_text('{"v": 1}')
    server = Server()
    server.downloads, server.on_download, server.lists = [], [], []
    server.incomplete = set()

    class E:
        pass

    e = E()
    e.tmp, e.home, e.store, e.records, e.cfg, e.server = (
        tmp_path, home, store, records, cfg, server)
    e.svc = RService(server)
    e.dest = tmp_path / "restored" / "brain.sqlite3"
    e.out_records = tmp_path / "restored" / "records"
    e.out_cfg = tmp_path / "restored" / "config.json"
    e.t = [OCT19]

    def cap():
        e.t[0] += 3600
        return bo.capture(home, store, KEY, records_dir=records, config_path=cfg,
                          settings=bo.Settings(base_threshold=100.0),
                          clock=lambda: e.t[0])

    def cap_up():
        item = cap()
        step()
        return item

    def step():
        res = bu.upload_step(
            home, drive_service=e.svc, transport=FakeTransport(server), key=KEY,
            budget_s=600, sleep=lambda s: None)
        assert res.error is None, res.error
        return res

    def restore(**kw):
        kw.setdefault("dest_store", e.dest)
        kw.setdefault("records_dir", e.out_records)
        kw.setdefault("config_path", e.out_cfg)
        return br.restore_latest(home, e.svc, KEY, **kw)

    def folder(name, parent):
        server.n += 1
        fid = f"dir-{server.n}"
        e.svc.meta[fid] = {"name": name, "parents": [parent], "mimeType": FOLDER_MIME}
        return fid

    def legacy(folder_id, created_ts, *, rows=30, records_text="legacy"):
        src = _make_db(tmp_path / f"legacy-{server.n}.sqlite3", rows=rows)
        rec = tmp_path / f"legacy-rec-{server.n}"
        rec.mkdir()
        (rec / "a.md").write_text(records_text)
        lcfg = tmp_path / f"legacy-cfg-{server.n}.json"
        lcfg.write_text('{"legacy": true}')
        out = tmp_path / f"legacy-{server.n}.enc"
        backup.make_encrypted_snapshot(src, out, KEY, records_dir=rec, config_path=lcfg)
        server.n += 1
        fid = f"leg-{server.n}"
        server.files[fid] = out.read_bytes()
        e.svc.meta[fid] = {"name": "snapshot.enc", "parents": [folder_id],
                           "createdTime": _iso(created_ts)}
        return fid, src

    e.cap, e.cap_up, e.step, e.restore, e.folder, e.legacy = (
        cap, cap_up, step, restore, folder, legacy)
    return e


def _sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT id, v FROM t ORDER BY id").fetchall()
    finally:
        con.close()


def _ids_named(e, prefix):
    return [fid for fid, m in e.svc._all().items() if m["name"].startswith(prefix)]


def test_rebuilds_byte_identical_store_from_base_and_deltas(env):
    base = env.cap_up()
    _insert(env.store, 40)
    env.cap_up()
    con = sqlite3.connect(env.store)
    con.execute("UPDATE t SET v = 'x' WHERE id <= 30")   # rewrite base pages
    con.commit()
    con.close()
    last = env.cap_up()
    assert last.kind == "delta" and last.seq == 3

    res = env.restore()
    assert res.used.kind == "manifest"
    assert res.used.captured_at == last.captured_at
    assert res.fallbacks == []
    assert res.path == env.dest
    assert _sha(env.dest) == last.db_sha256
    assert env.dest.stat().st_size == last.page_count * last.page_size
    assert _rows(env.dest) == _rows(env.store)
    assert base.chain in res.used.label
    # nothing left behind: no stage, no downloads, no decrypted records
    assert not env.dest.with_name(env.dest.name + ".stage").exists()
    assert not (bo.backup_dir(env.home) / "restore").exists()


def test_store_shrink_truncates_to_page_count(env):
    env.cap()
    con = sqlite3.connect(env.store)
    con.execute("DELETE FROM t WHERE id > 20")
    con.commit()
    con.execute("VACUUM")
    con.close()
    last = env.cap()
    env.step()
    env.restore()
    assert env.dest.stat().st_size == last.page_count * last.page_size
    assert _sha(env.dest) == last.db_sha256


def test_newest_restore_point_wins_across_locations(env):
    env.cap()
    env.step()                                  # manifest captured 2026-10-19
    esc_person = env.folder("owner@example.org", "ESC")
    june = datetime(2026, 6, 16, tzinfo=timezone.utc).timestamp()
    env.legacy(esc_person, june)

    cands = br.candidates(env.home, env.svc, KEY)
    assert [c.kind for c in cands] == ["manifest", "legacy"]
    assert cands[0].captured_at > cands[1].captured_at == june

    res = env.restore()
    assert res.used.kind == "manifest"
    assert _rows(env.dest) == _rows(env.store)


def test_newer_legacy_file_beats_older_manifest(env):
    env.cap()
    env.step()
    user_dir = env.folder("owner", "0Adrive")
    newer = OCT19 + 30 * 86400
    _fid, src = env.legacy(user_dir, newer, rows=7)

    res = env.restore()
    assert res.used.kind == "legacy" and res.used.captured_at == newer
    assert res.used.folder_id == user_dir
    assert _rows(env.dest) == _rows(src)


def test_legacy_file_loose_in_person_folder_is_a_candidate(env):
    env.cap()
    env.step()
    person = env.svc.by_name("owner@example.org")[0]
    env.legacy(person, OCT19 + 86400 * 2, rows=5)
    cands = br.candidates(env.home, env.svc, KEY)
    assert cands[0].kind == "legacy" and cands[0].folder_id == person


def test_corrupt_delta_falls_back_to_previous_manifest(env, caplog):
    env.cap_up()
    _insert(env.store, 40)
    first_delta = env.cap_up()
    _insert(env.store, 40)
    newest = env.cap_up()
    seg_name = newest.segments[0]["name"]
    fid = env.svc.by_name(seg_name)[0]
    data = bytearray(env.server.files[fid])
    data[len(data) // 2] ^= 0xFF
    env.server.files[fid] = bytes(data)

    caplog.set_level("WARNING")
    res = env.restore()
    assert res.used.kind == "manifest"
    assert res.used.captured_at == first_delta.captured_at
    assert _sha(env.dest) == first_delta.db_sha256
    assert len(res.fallbacks) == 1
    skipped, why = res.fallbacks[0]
    assert skipped.captured_at == newest.captured_at
    assert "sha256" in why and seg_name in why
    assert any("sha256" in r.message for r in caplog.records)


def test_broken_chain_falls_back_to_legacy(env):
    base = env.cap()
    _insert(env.store, 40)
    env.cap()
    env.step()
    esc_person = env.folder("owner@example.org", "ESC")
    _fid, src = env.legacy(esc_person, datetime(2026, 6, 16, tzinfo=timezone.utc).timestamp(),
                           rows=9)
    base_id = env.svc.by_name(base.segments[0]["name"])[0]
    del env.server.files[base_id]

    res = env.restore()
    assert res.used.kind == "legacy"
    assert _rows(env.dest) == _rows(src)
    assert [c.kind for c, _ in res.fallbacks] == ["manifest", "manifest"]
    assert all("missing" in why for _, why in res.fallbacks)


def test_no_restore_point_raises_and_reports_every_candidate(env):
    base = env.cap()
    env.step()
    del env.server.files[env.svc.by_name(base.segments[0]["name"])[0]]
    with pytest.raises(br.NoRestorePoint) as ei:
        env.restore()
    assert len(ei.value.fallbacks) == 1
    assert not env.dest.exists()


def test_unreadable_manifest_is_skipped_for_the_previous_one(env):
    first = env.cap()
    _insert(env.store, 10)
    env.cap()
    env.step()
    mid = env.svc.by_name(f"manifest-{first.chain}-000002")[0]
    env.server.files[mid] = b"garbage"
    res = env.restore()
    assert res.used.captured_at == first.captured_at
    assert len(res.fallbacks) == 1 and "manifest" in res.fallbacks[0][1]


def test_duplicate_manifests_for_one_seq_are_one_candidate(env):
    base = env.cap()
    env.step()
    mid = env.svc.by_name(f"manifest-{base.chain}-000001")[0]
    env.server.n += 1
    dup = f"svc-{env.server.n}"
    env.server.files[dup] = env.server.files[mid]
    env.svc.meta[dup] = dict(env.svc._all()[mid])
    cands = br.candidates(env.home, env.svc, KEY)
    assert len(cands) == 1
    res = env.restore()
    assert res.fallbacks == []


def test_older_manifests_decrypted_only_when_falling_back(env):
    base = env.cap_up()
    for _ in range(3):
        _insert(env.store, 5)
        env.cap_up()
    manifest_ids = set(_ids_named(env, "manifest-"))
    assert len(manifest_ids) == 4
    br.candidates(env.home, env.svc, KEY)
    assert len(manifest_ids & set(env.server.downloads)) == 1
    env.server.downloads.clear()
    env.restore()
    assert manifest_ids & set(env.server.downloads) == set()   # cached
    assert base.chain


def test_download_resumes_from_files_already_verified(env):
    env.cap_up()
    _insert(env.store, 40)
    env.cap_up()
    _insert(env.store, 40)
    last = env.cap_up()
    data_ids = set(_ids_named(env, "base-") + _ids_named(env, "delta-")
                   + _ids_named(env, "records-"))
    assert len(data_ids) >= 4
    clock = [0.0]
    seen: list[str] = []

    def hook(fid):
        if fid in data_ids:
            seen.append(fid)
            if len(seen) == 2:
                clock[0] = 1e6
    env.server.on_download.append(hook)

    with pytest.raises(br.RestoreIncomplete):
        env.restore(budget_s=10, clock=lambda: clock[0])
    assert len(set(seen)) == 2
    assert not env.dest.exists()
    first_two = set(seen)

    env.server.on_download.clear()
    env.server.downloads.clear()
    res = env.restore()
    again = set(env.server.downloads) & data_ids
    assert again == data_ids - first_two
    # the manifest JSON was cached too
    assert not set(env.server.downloads) & set(_ids_named(env, "manifest-"))
    assert _sha(env.dest) == last.db_sha256 and res.fallbacks == []


def test_records_and_config_restored_on_every_path(env):
    env.cap()
    (env.records / "a.md").write_text("v2")
    env.cfg.write_text('{"v": 2}')
    _insert(env.store, 5)
    env.cap()
    env.step()
    env.restore()
    assert (env.out_records / "a.md").read_text() == "v2"
    assert json.loads(env.out_cfg.read_text()) == {"v": 2}

    # legacy path (newer legacy bundle) restores its records + config too
    env.dest.unlink()
    user_dir = env.folder("owner", "0Adrive")
    env.legacy(user_dir, OCT19 + 90 * 86400, records_text="from-legacy")
    res = env.restore()
    assert res.used.kind == "legacy"
    assert (env.out_records / "a.md").read_text() == "from-legacy"
    assert json.loads(env.out_cfg.read_text()) == {"legacy": True}


def _corrupt_last_page(path):
    con = sqlite3.connect(path)
    ps = con.execute("PRAGMA page_size").fetchone()[0]
    n = con.execute("PRAGMA page_count").fetchone()[0]
    con.close()
    with open(path, "r+b") as f:
        f.seek((n - 1) * ps + 8)           # leaf cell-pointer array
        f.write(b"\xff\xf0" * 4)


def test_never_touches_dest_until_verified(env):
    _corrupt_last_page(env.store)
    env.cap()
    env.step()
    env.dest.parent.mkdir(parents=True)
    original = _make_db(env.dest, rows=3).read_bytes()
    swaps = []

    with pytest.raises(br.NoRestorePoint) as ei:
        env.restore(swap=lambda s, d: swaps.append((s, d)))
    assert "integrity" in ei.value.fallbacks[0][1]
    assert swaps == []
    assert env.dest.read_bytes() == original
    assert not env.dest.with_name(env.dest.name + ".stage").exists()
    assert not env.out_records.exists() and not env.out_cfg.exists()


def test_db_sha_mismatch_never_swaps(env, monkeypatch):
    env.cap()
    env.step()
    real = br._sha256_file
    monkeypatch.setattr(br, "_sha256_file",
                        lambda p: "0" * 64 if str(p).endswith(".stage") else real(p))
    swaps = []
    with pytest.raises(br.NoRestorePoint) as ei:
        env.restore(swap=lambda s, d: swaps.append(1))
    assert "db_sha256" in ei.value.fallbacks[0][1] and swaps == []


def test_transient_download_error_propagates_instead_of_demoting(env):
    """A network failure says nothing about the restore point: restoring an
    older one (the June legacy file) instead would lose months of data."""
    base = env.cap()
    env.step()
    esc_person = env.folder("owner@example.org", "ESC")
    env.legacy(esc_person, datetime(2026, 6, 16, tzinfo=timezone.utc).timestamp())
    seg = env.svc.by_name(base.segments[0]["name"])[0]

    def boom(fid):
        if fid == seg:
            raise httplib2.ServerNotFoundError("offline")
    env.server.on_download.append(boom)
    with pytest.raises(httplib2.ServerNotFoundError):
        env.restore()
    assert not env.dest.exists()


def test_swap_gets_stage_and_dest(env):
    env.cap()
    env.step()
    calls = []

    def swap(stage, dest):
        calls.append((stage, dest))
        backup.swap_in_store(stage, dest)
    env.restore(swap=swap)
    assert calls == [(env.dest.with_name(env.dest.name + ".stage"), env.dest)]


def test_stale_stage_removed_first(env):
    env.cap()
    env.step()
    env.dest.parent.mkdir(parents=True)
    stage = env.dest.with_name(env.dest.name + ".stage")
    stage.write_bytes(b"x" * 100_000)
    env.restore()
    assert not stage.exists()
    assert _rows(env.dest) == _rows(env.store)


# ---- fix round 1


def test_local_disk_error_building_stage_propagates_without_fallback(env, monkeypatch):
    """ENOSPC writing the stage is this machine's problem: it must not demote
    to an older (smaller) restore point such as the June legacy file."""
    env.cap_up()
    esc_person = env.folder("owner@example.org", "ESC")
    env.legacy(esc_person, datetime(2026, 6, 16, tzinfo=timezone.utc).timestamp())
    env.dest.parent.mkdir(parents=True)
    original = _make_db(env.dest, rows=3).read_bytes()
    real_open = br._open_stage

    class Full:
        def __init__(self, fh):
            self.fh = fh

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            self.fh.close()

        def seek(self, *a):
            return self.fh.seek(*a)

        def truncate(self, *a):
            return self.fh.truncate(*a)

        def write(self, data):
            self.fh.write(data[:10])
            raise OSError(errno.ENOSPC, "No space left on device")

    monkeypatch.setattr(br, "_open_stage", lambda p: Full(real_open(p)))
    swaps = []
    with pytest.raises(OSError) as ei:
        env.restore(swap=lambda s, d: swaps.append(1))
    assert ei.value.errno == errno.ENOSPC
    assert swaps == []
    assert env.dest.read_bytes() == original
    assert not env.dest.with_name(env.dest.name + ".stage").exists()
    assert not list(br.restore_dir(env.home).glob("work-*"))
    assert not env.out_records.exists()


def test_incomplete_person_folder_listing_raises_transient(env):
    env.cap_up()
    person = env.svc.by_name("owner@example.org")[0]
    env.server.incomplete.add(person)
    with pytest.raises(br.ListingIncomplete):
        env.restore()
    with pytest.raises(br.ListingIncomplete):
        br.candidates(env.home, env.svc, KEY)
    assert not env.dest.exists()


def test_incomplete_chain_listing_raises_transient(env):
    base = env.cap_up()
    _insert(env.store, 5)
    env.cap_up()
    env.server.incomplete.add(env.svc.by_name(base.chain)[0])
    with pytest.raises(br.ListingIncomplete):
        env.restore()
    assert not env.dest.exists()
    assert issubclass(br.ListingIncomplete, TimeoutError)


def test_records_placement_failure_keeps_original_records(env, monkeypatch):
    env.cap_up()
    env.out_records.mkdir(parents=True)
    (env.out_records / "keep.md").write_text("original")
    env.out_cfg.write_text('{"orig": 1}')
    real = br.shutil.copytree

    def half(src, dst, *a, **kw):
        if Path(dst).parent != env.out_records.parent:    # not the placement
            return real(src, dst, *a, **kw)
        Path(dst).mkdir(parents=True)
        (Path(dst) / "partial.md").write_text("x")
        raise OSError(errno.EIO, "copy failed mid-way")
    monkeypatch.setattr(br.shutil, "copytree", half)
    with pytest.raises(OSError):
        env.restore()
    monkeypatch.setattr(br.shutil, "copytree", real)
    assert [p.name for p in env.out_records.iterdir()] == ["keep.md"]
    assert (env.out_records / "keep.md").read_text() == "original"
    assert _rows(env.dest) == _rows(env.store)       # the store did swap
    assert sorted(p.name for p in env.out_records.parent.iterdir()) == [
        "brain.sqlite3", "config.json", "records"]


def test_records_placement_replaces_existing_dir(env):
    env.cap_up()
    env.out_records.mkdir(parents=True)
    (env.out_records / "stale.md").write_text("old")
    env.restore()
    assert sorted(p.name for p in env.out_records.iterdir()) == ["a.md"]
    assert sorted(p.name for p in env.out_records.parent.iterdir()) == [
        "brain.sqlite3", "config.json", "records"]


def test_candidates_unwraps_transient_errors(env):
    base = env.cap_up()
    mid = env.svc.by_name(f"manifest-{base.chain}-000001")[0]

    def boom(fid):
        if fid == mid:
            raise httplib2.ServerNotFoundError("offline")
    env.server.on_download.append(boom)
    with pytest.raises(httplib2.ServerNotFoundError):
        br.candidates(env.home, env.svc, KEY)


# ---- final review: local trouble is never evidence against a restore point


def test_local_disk_error_writing_decrypted_manifest_propagates(env, monkeypatch):
    """ENOSPC writing the decrypted manifest (.plain) used to mark the
    manifest unreadable and demote to an older point."""
    env.cap_up()
    esc_person = env.folder("owner@example.org", "ESC")
    env.legacy(esc_person, datetime(2026, 6, 16, tzinfo=timezone.utc).timestamp())
    real = backup.decrypt_file

    def full(src, dst, key):
        if str(dst).endswith(".plain"):
            raise OSError(errno.ENOSPC, "No space left on device")
        return real(src, dst, key)
    monkeypatch.setattr(br.backup, "decrypt_file", full)
    swaps = []
    with pytest.raises(OSError) as ei:
        env.restore(swap=lambda s, d: swaps.append(1))
    assert ei.value.errno == errno.ENOSPC
    assert swaps == []


@pytest.mark.parametrize("msg,code", [
    ("disk I/O error", errno.EIO),
    ("unable to open database file", errno.EIO),
    ("database or disk is full", errno.ENOSPC),
])
def test_integrity_local_sqlite_error_is_os_error(tmp_path, monkeypatch, msg, code):
    db = _make_db(tmp_path / "s.sqlite3")

    def connect(*a, **k):
        raise sqlite3.OperationalError(msg)
    monkeypatch.setattr(br.sqlite3, "connect", connect)
    with pytest.raises(OSError) as ei:
        br._integrity(db)
    assert ei.value.errno == code
    assert br._is_local_env_error(ei.value)


def test_integrity_corruption_is_still_bad(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "s.sqlite3")

    def connect(*a, **k):
        raise sqlite3.DatabaseError("database disk image is malformed")
    monkeypatch.setattr(br.sqlite3, "connect", connect)
    with pytest.raises(br._Bad):
        br._integrity(db)


def test_local_disk_error_in_integrity_check_propagates_without_fallback(env, monkeypatch):
    env.cap_up()
    esc_person = env.folder("owner@example.org", "ESC")
    env.legacy(esc_person, datetime(2026, 6, 16, tzinfo=timezone.utc).timestamp())
    real = br.sqlite3.connect

    def connect(target, *a, **k):
        if "immutable=1" in str(target):
            raise sqlite3.OperationalError("disk I/O error")
        return real(target, *a, **k)
    monkeypatch.setattr(br.sqlite3, "connect", connect)
    swaps = []
    with pytest.raises(OSError):
        env.restore(swap=lambda s, d: swaps.append(1))
    assert swaps == []
