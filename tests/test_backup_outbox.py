"""Local page-delta capture into the backup outbox (incremental backup, Task 3)."""
import errno
import hashlib
import json
import os
import shutil
import sqlite3
from contextlib import contextmanager

import pytest

from mcpbrain import backup, backup_outbox as bo, backup_pages as bp


KEY = backup.generate_escrow_key()


def _make_db(path, rows=200, page_size=4096):
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA page_size={page_size}")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)",
                    [(os.urandom(200).hex(),) for _ in range(rows)])
    con.commit()
    con.close()
    return path


def _insert(path, n=1):
    con = sqlite3.connect(path)
    con.executemany("INSERT INTO t (v) VALUES (?)",
                    [(os.urandom(200).hex(),) for _ in range(n)])
    con.commit()
    con.close()


def _update_row(path, rowid, value):
    con = sqlite3.connect(path)
    con.execute("UPDATE t SET v=? WHERE id=?", (value, rowid))
    con.commit()
    con.close()


@pytest.fixture
def env(tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    store = _make_db(tmp_path / "brain.sqlite3")
    records = tmp_path / "records"
    records.mkdir()
    (records / "a.md").write_text("hello")
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")

    def cap(**kw):
        kw.setdefault("settings", bo.Settings())
        return bo.capture(home, store, KEY, records_dir=records,
                          config_path=cfg, **kw)

    return home, store, records, cfg, cap


def _rebuild(home, items):
    """Apply items' segments in order and return the rebuilt file bytes."""
    out = bytearray()
    last = items[-1]
    for it in items:
        for seg in it.segments:
            hdr, pages = bp.read_segment(bo.outbox_dir(home) / seg["file"], KEY)
            with pages:
                for pgno, page in pages:
                    off = (pgno - 1) * it.page_size
                    if len(out) < off + it.page_size:
                        out.extend(b"\x00" * (off + it.page_size - len(out)))
                    out[off:off + it.page_size] = page
    return bytes(out[: last.page_count * last.page_size])


def _rows(path):
    con = sqlite3.connect(path)
    try:
        return con.execute("SELECT id, v FROM t ORDER BY id").fetchall()
    finally:
        con.close()


def _rows_of_bytes(data, home):
    """The online-backup copy differs from the source in header bytes, so
    compare what a restore yields logically, not the live file's raw bytes."""
    p = home / "rebuilt.sqlite3"
    p.write_bytes(data)
    try:
        return _rows(p)
    finally:
        p.unlink()


def _all_pages(item):
    return sorted(p for s in item.segments
                  for p in range(s["first_pgno"], s["last_pgno"] + 1))


def test_first_capture_is_base_with_every_page(env):
    home, store, _r, _c, cap = env
    item = cap()
    assert item.kind == "base"
    assert item.seq == 1 and item.prev_seq is None
    assert item.chain.startswith("chain-")
    assert item.name == f"{item.chain}-000001"
    assert sum(s["pages"] for s in item.segments) == item.page_count
    assert all(s["name"].startswith("base-") for s in item.segments)
    assert item.records is not None and item.records["name"] == "records-000001"
    rebuilt = _rebuild(home, [item])
    assert hashlib.sha256(rebuilt).hexdigest() == item.db_sha256
    assert (bo.outbox_dir(home) / f"{item.name}.json").exists()
    assert (bo.backup_dir(home) / "pages.idx").exists()
    assert (bo.backup_dir(home) / "capture.json").exists()
    assert list(bo.work_dir(home).iterdir()) == []
    # sidecar round-trips
    side = bo.outbox_dir(home) / f"{item.name}.json"
    assert bo.Item.from_json(side.read_text()) == item


def test_second_capture_is_delta_with_only_changed_pages(env):
    home, store, _r, _c, cap = env
    base = cap()
    _insert(store)
    d = cap()
    assert d.kind == "delta" and d.chain == base.chain
    assert d.seq == 2 and d.prev_seq == 1
    n = sum(s["pages"] for s in d.segments)
    assert 0 < n < d.page_count
    assert all(s["name"].startswith("delta-000002-") for s in d.segments)
    assert d.records is None
    rebuilt = _rebuild(home, [base, d])
    assert hashlib.sha256(rebuilt).hexdigest() == d.db_sha256
    assert _rows_of_bytes(rebuilt, home) == _rows(store)


def test_no_change_returns_none(env):
    home, store, _r, _c, cap = env
    cap()
    before = sorted(p.name for p in bo.outbox_dir(home).iterdir())
    assert cap() is None
    assert sorted(p.name for p in bo.outbox_dir(home).iterdir()) == before
    assert list(bo.work_dir(home).iterdir()) == []


def test_records_item_only_when_fingerprint_changes(env):
    home, store, records, _c, cap = env
    cap()
    (records / "b.md").write_text("new note")
    d = cap()
    assert d is not None and d.kind == "delta"
    assert d.segments == []
    assert d.records["name"] == f"records-{d.seq:06d}"
    f = bo.outbox_dir(home) / d.records["file"]
    assert f.stat().st_size == d.records["size"]
    assert hashlib.md5(f.read_bytes()).hexdigest() == d.records["md5"]
    assert hashlib.sha256(f.read_bytes()).hexdigest() == d.records["sha256"]
    _insert(store)
    d2 = cap()
    assert d2.records is None


def test_store_replaced_forces_base(env, tmp_path):
    home, store, _r, _c, cap = env
    base = cap()
    _insert(store)
    copy = tmp_path / "swap.sqlite3"
    shutil.copy2(store, copy)
    os.replace(copy, store)
    item = cap()
    assert item.kind == "base"
    assert item.chain != base.chain
    assert item.seq == 1 and item.prev_seq is None
    # both chains' base segments coexist locally without colliding
    names = [s["file"] for s in base.segments + item.segments]
    assert len(set(names)) == len(names)
    for it in (base, item):
        for s in it.segments:
            assert (bo.outbox_dir(home) / s["file"]).exists()


def test_page_size_change_forces_base(env, tmp_path):
    home, store, _r, _c, cap = env
    base = cap()
    con = sqlite3.connect(store)
    con.execute("PRAGMA page_size=8192")
    con.execute("VACUUM")
    con.close()
    item = cap()
    assert item.kind == "base" and item.chain != base.chain
    assert item.page_size == 8192


def test_delta_bytes_over_threshold_forces_base(env):
    home, store, _r, _c, cap = env
    base = cap(settings=bo.Settings(base_threshold=0.0))
    _insert(store)
    item = cap(settings=bo.Settings(base_threshold=0.0))
    assert item.kind == "base" and item.chain != base.chain
    assert sum(s["pages"] for s in item.segments) == item.page_count


def test_crash_before_index_save_redoes_capture(env, monkeypatch):
    home, store, _r, _c, cap = env
    base = cap()
    original = dict(_rows(store))[5]
    _update_row(store, 5, "x" * 400)
    real_save = bp.PageIndex.save
    calls = {"n": 0}

    def boom(self, path):
        calls["n"] += 1
        raise OSError("disk went away")

    monkeypatch.setattr(bp.PageIndex, "save", boom)
    with pytest.raises(OSError):
        cap()
    monkeypatch.setattr(bp.PageIndex, "save", real_save)
    failed = [it for it in bo.pending_items(home) if it.seq == 2][0]
    failed_pages = set(_all_pages(failed))

    # orphans a crash could leave: a temp file and a segment no sidecar names
    (bo.outbox_dir(home) / "x-base-0001.tmp").write_bytes(b"junk")
    (bo.outbox_dir(home) / f"{base.chain}-000009-abc-delta-000009-0001").write_bytes(b"junk")
    (bo.work_dir(home) / "capture.sqlite3").write_bytes(b"junk")

    # the row changes BACK to its pre-item-2 bytes: diffing against the stale
    # index alone would see no change on its page, yet item 2 holds "x" there,
    # so the redo must still carry that page
    _update_row(store, 5, original)
    redo = cap()
    assert redo.kind == "delta" and redo.chain == base.chain
    assert redo.prev_seq == 2 and redo.seq == 3
    assert failed_pages <= set(_all_pages(redo))
    rebuilt = _rebuild(home, [base, failed, redo])
    assert hashlib.sha256(rebuilt).hexdigest() == redo.db_sha256
    assert _rows_of_bytes(rebuilt, home) == _rows(store)
    names = {p.name for p in bo.outbox_dir(home).iterdir()}
    assert "x-base-0001.tmp" not in names
    assert f"{base.chain}-000009-abc-delta-000009-0001" not in names
    assert list(bo.work_dir(home).iterdir()) == []


def test_crash_after_index_save_before_state_is_still_correct(env, monkeypatch):
    """pages.idx advanced but capture.json did not: a revert must not be lost."""
    home, store, _r, _c, cap = env
    base = cap()
    _update_row(store, 5, "x" * 400)
    monkeypatch.setattr(bo, "_save_state", lambda *a, **k: (_ for _ in ()).throw(
        OSError("crash")))
    with pytest.raises(OSError):
        cap()
    monkeypatch.undo()
    failed = [it for it in bo.pending_items(home) if it.seq == 2][0]
    con = sqlite3.connect(store)
    con.execute("UPDATE t SET v=v||'z' WHERE id=150")
    con.commit()
    con.close()
    redo = cap()
    assert redo.prev_seq == 2 and redo.seq == 3
    rebuilt = _rebuild(home, [base, failed, redo])
    assert hashlib.sha256(rebuilt).hexdigest() == redo.db_sha256


def test_low_disk_refuses_before_copy(env, monkeypatch):
    home, store, _r, _c, cap = env
    usage = shutil.disk_usage(str(store.parent))
    monkeypatch.setattr(
        bo.shutil, "disk_usage",
        lambda p: type(usage)(usage.total, usage.total - 10, 10))
    with pytest.raises(OSError) as ei:
        cap()
    assert ei.value.errno == errno.ENOSPC
    assert not bo.work_dir(home).exists() or list(bo.work_dir(home).iterdir()) == []
    assert not (bo.backup_dir(home) / "capture.json").exists()


def test_outbox_full_pauses_capture(env):
    home, store, _r, _c, cap = env
    item = cap()
    before = {p.name: p.read_bytes() for p in bo.outbox_dir(home).iterdir()}
    _insert(store)
    with pytest.raises(bo.OutboxFull):
        cap(settings=bo.Settings(outbox_max_bytes=1))
    after = {p.name: p.read_bytes() for p in bo.outbox_dir(home).iterdir()}
    assert after == before
    assert [i.name for i in bo.pending_items(home)] == [item.name]
    assert bo.outbox_bytes(home) == sum(len(v) for v in before.values())


def test_lock_busy_propagates_before_touching_anything(env):
    home, store, _r, _c, cap = env
    cap()
    _insert(store)

    def snap():
        out = {}
        for d in (bo.outbox_dir(home), bo.work_dir(home)):
            for p in d.iterdir():
                out[str(p)] = (p.stat().st_mtime_ns, p.read_bytes())
        st = bo.backup_dir(home) / "capture.json"
        out["state"] = st.read_bytes()
        out["idx"] = (bo.backup_dir(home) / "pages.idx").read_bytes()
        return out

    before = snap()

    @contextmanager
    def busy():
        raise bo.LockBusy()
        yield  # pragma: no cover

    with pytest.raises(bo.LockBusy):
        cap(lock=busy())
    assert snap() == before


def test_copy_holds_lock_only_for_backup(env, monkeypatch):
    home, store, _r, _c, cap = env
    log = []
    real_copy = bo._copy_store
    real_scan = bp.scan_changes

    def copy(*a, **k):
        log.append("copy")
        return real_copy(*a, **k)

    def scan(*a, **k):
        log.append("scan")
        return real_scan(*a, **k)

    monkeypatch.setattr(bo, "_copy_store", copy)
    monkeypatch.setattr(bp, "scan_changes", scan)

    @contextmanager
    def lock():
        log.append("enter")
        try:
            yield
        finally:
            log.append("exit")

    cap(lock=lock())
    assert log[:4] == ["enter", "copy", "exit", "scan"]


def _three_deltas(env):
    home, store, _r, _c, cap = env
    base = cap()
    items = []
    for v in ("a", "b", "c"):
        _update_row(store, 5, v * 400)
        items.append(cap())
    return home, store, base, items


def test_coalesce_newest_page_wins(env):
    home, store, base, (d2, d3, d4) = _three_deltas(env)
    merged = bo.coalesce(home, KEY, started=set())
    assert merged is not None
    assert merged.kind == "delta" and merged.chain == base.chain
    assert merged.seq == d4.seq and merged.prev_seq == d2.prev_seq == 1
    assert merged.db_sha256 == d4.db_sha256
    assert merged.index_digest == d4.index_digest != ""
    assert merged.page_count == d4.page_count
    assert all(s["name"].startswith(f"delta-{d4.seq:06d}-")
               for s in merged.segments)
    pend = bo.pending_items(home)
    assert [i.name for i in pend] == [base.name, merged.name]
    rebuilt = _rebuild(home, [base, merged])
    assert hashlib.sha256(rebuilt).hexdigest() == d4.db_sha256
    # every file left in the outbox is named by a sidecar
    named = {f"{i.name}.json" for i in pend}
    for i in pend:
        named |= {s["file"] for s in i.segments}
        if i.records:
            named.add(i.records["file"])
    assert {p.name for p in bo.outbox_dir(home).iterdir()} == named
    assert bo.coalesce(home, KEY, started=set()) is None


def test_coalesce_does_not_merge_across_started(env):
    home, store, base, (d2, d3, d4) = _three_deltas(env)
    assert bo.coalesce(home, KEY, started={d3.name}) is None
    assert [i.name for i in bo.pending_items(home)] == [
        base.name, d2.name, d3.name, d4.name]


def test_coalesce_skips_started_and_merges_the_rest(env):
    home, store, _r, _c, cap = env
    base = cap()
    ds = []
    for v in ("a", "b", "c", "d"):
        _update_row(store, 5, v * 400)
        ds.append(cap())
    merged = bo.coalesce(home, KEY, started={ds[0].name})
    assert merged.seq == ds[3].seq and merged.prev_seq == ds[0].seq
    pend = bo.pending_items(home)
    assert [i.name for i in pend] == [base.name, ds[0].name, merged.name]
    rebuilt = _rebuild(home, pend)
    assert hashlib.sha256(rebuilt).hexdigest() == ds[3].db_sha256


def test_coalesce_keeps_newest_records(env):
    home, store, records, _c, cap = env
    cap()
    (records / "b.md").write_text("one")
    r1 = cap()
    _insert(store)
    d = cap()
    merged = bo.coalesce(home, KEY, started=set())
    assert merged.records == r1.records
    assert merged.seq == d.seq
    assert (bo.outbox_dir(home) / merged.records["file"]).exists()


def test_crash_after_coalesce_commit_hides_superseded(env):
    home, store, base, (d2, d3, d4) = _three_deltas(env)
    side = {i.name: (bo.outbox_dir(home) / f"{i.name}.json").read_text()
            for i in (d2, d3)}
    merged = bo.coalesce(home, KEY, started=set())
    # simulate a crash after the merged sidecar committed but before the older
    # sidecars were deleted
    for name, text in side.items():
        (bo.outbox_dir(home) / f"{name}.json").write_text(text)
    assert [i.name for i in bo.pending_items(home)] == [base.name, merged.name]
    _insert(store)
    nxt = env[4]()
    assert nxt.prev_seq == merged.seq
    assert not (bo.outbox_dir(home) / f"{d2.name}.json").exists()


def test_settings_from_config():
    s = bo.Settings.from_config({"retain": 3, "interval_s": 5,
                                 "base_threshold": 0.25})
    assert s.retain_days == 3 and s.base_threshold == 0.25
    assert s.capture_interval_s == 3600 and s.upload_budget_s == 60
    assert s.outbox_max_bytes == 2_000_000_000
    s = bo.Settings.from_config({"retain": 3, "retain_days": 9})
    assert s.retain_days == 9
    assert bo.Settings.from_config(None) == bo.Settings()
    assert bo.Settings.from_config({}).retain_days == 7


def test_item_json_round_trip():
    it = bo.Item(name="chain-20261019-abcdef-000002", kind="delta",
                 chain="chain-20261019-abcdef", seq=2, prev_seq=1,
                 captured_at=1.5, page_size=4096, page_count=10,
                 db_sha256="00", segments=[{"name": "delta-000002-0001"}],
                 records=None, bytes=7)
    assert bo.Item.from_json(it.to_json()) == it
    assert json.loads(it.to_json())["kind"] == "delta"
    extra = json.loads(it.to_json())
    extra["future_field"] = 1
    assert bo.Item.from_json(json.dumps(extra)) == it


def _pages_of_chain(home, item):
    """Every pending item of `item`'s chain up to and including it."""
    return [i for i in bo.pending_items(home)
            if i.chain == item.chain and i.seq <= item.seq]


def _assert_rebuilds(home, item, store):
    chain = _pages_of_chain(home, item)
    assert chain[0].kind == "base"
    rebuilt = _rebuild(home, chain)
    assert hashlib.sha256(rebuilt).hexdigest() == item.db_sha256
    assert _rows_of_bytes(rebuilt, home) == _rows(store)


def _threshold_base_crash(env, monkeypatch, where):
    home, store, _r, _c, cap = env
    cap()
    _update_row(store, 5, "x" * 400)
    if where == "after_sidecar":
        monkeypatch.setattr(bp.PageIndex, "save", lambda self, p: (_ for _ in ()).throw(
            OSError("crash")))
    else:
        monkeypatch.setattr(bo, "_save_state", lambda *a, **k: (_ for _ in ()).throw(
            OSError("crash")))
    with pytest.raises(OSError):
        cap(settings=bo.Settings(base_threshold=0.0))
    monkeypatch.undo()
    _update_row(store, 190, "y" * 400)  # a page far from row 5's
    return home, store, cap()


@pytest.mark.parametrize("where", ["after_sidecar", "after_index"])
def test_crash_inside_threshold_base_commit_next_item_rebuilds(env, monkeypatch, where):
    home, store, item = _threshold_base_crash(env, monkeypatch, where)
    _assert_rebuilds(home, item, store)
    _update_row(store, 9, "z" * 400)
    _assert_rebuilds(home, env[4](), store)


@pytest.mark.parametrize("where", ["after_sidecar", "after_index"])
def test_crash_inside_first_base_commit_next_item_rebuilds(env, monkeypatch, where):
    home, store, _r, _c, cap = env
    if where == "after_sidecar":
        monkeypatch.setattr(bp.PageIndex, "save", lambda self, p: (_ for _ in ()).throw(
            OSError("crash")))
    else:
        monkeypatch.setattr(bo, "_save_state", lambda *a, **k: (_ for _ in ()).throw(
            OSError("crash")))
    with pytest.raises(OSError):
        cap()
    monkeypatch.undo()
    _update_row(store, 7, "y" * 400)
    item = cap()
    _assert_rebuilds(home, item, store)


def test_index_not_matching_state_forces_base(env):
    """pages.idx that is not the state's index (nor a pending item's) is never
    diffed against: e.g. the item it described was uploaded and deleted."""
    home, store, _r, _c, cap = env
    base = cap()
    idx = bo.backup_dir(home) / "pages.idx"
    data = bytearray(idx.read_bytes())
    data[-1] ^= 0xFF
    idx.write_bytes(bytes(data))
    _insert(store)
    item = cap()
    assert item.kind == "base" and item.chain != base.chain


def test_index_matching_state_continues_after_sidecar_uploaded(env):
    home, store, _r, _c, cap = env
    base = cap()
    _insert(store)
    d = cap()
    # the uploader shipped both items and deleted their local files
    for it in (base, d):
        for f in it.files():
            (bo.outbox_dir(home) / f).unlink()
        (bo.outbox_dir(home) / f"{it.name}.json").unlink()
    _insert(store)
    nxt = cap()
    assert nxt.kind == "delta" and nxt.prev_seq == d.seq


def test_free_space_need_uses_logical_page_count(env, monkeypatch):
    home, store, _r, _c, cap = env
    con = sqlite3.connect(store)
    ps = con.execute("PRAGMA page_size").fetchone()[0]
    n = con.execute("PRAGMA page_count").fetchone()[0]
    con.close()
    need = int(n * ps * 1.1)
    # main file reports tiny; the logical size must still drive the check
    real_getsize = os.path.getsize
    monkeypatch.setattr(bo.os.path, "getsize",
                        lambda p: 1 if str(p) == str(store) else real_getsize(p))
    usage = shutil.disk_usage(str(store.parent))
    monkeypatch.setattr(bo.shutil, "disk_usage",
                        lambda p: type(usage)(usage.total, 0, need - 1))
    with pytest.raises(OSError) as ei:
        cap()
    assert ei.value.errno == errno.ENOSPC
    monkeypatch.setattr(bo.shutil, "disk_usage",
                        lambda p: type(usage)(usage.total, 0, need + 1))
    assert cap() is not None


@pytest.mark.parametrize("bad", ["{not json", '{"name": "x"}'])
def test_unparseable_sidecar_quarantined_and_files_kept(env, bad):
    home, store, _r, _c, cap = env
    item = cap()
    side = bo.outbox_dir(home) / f"{item.name}.json"
    side.write_text(bad)
    orphan = bo.outbox_dir(home) / "someone-else-delta-000003-0001"
    orphan.write_bytes(b"maybe named by the bad sidecar")
    _insert(store)
    cap()
    ob = bo.outbox_dir(home)
    assert not side.exists()
    assert (ob / f"{item.name}.json.bad").read_text() == bad
    assert orphan.exists()
    for f in item.files():
        assert (ob / f).exists()
    # and stays protected on later sweeps
    _insert(store)
    cap()
    assert orphan.exists()
