"""Page index, page scan and encrypted page segments (incremental backup)."""
import hashlib
import json
import zlib
import os
import sqlite3
import tarfile

import pytest
from cryptography.fernet import InvalidToken

from mcpbrain import backup, backup_pages as bp


def _make_db(path, rows=400, page_size=4096):
    con = sqlite3.connect(path)
    con.execute(f"PRAGMA page_size={page_size}")
    con.execute("CREATE TABLE t (id INTEGER PRIMARY KEY, v TEXT)")
    con.executemany("INSERT INTO t (v) VALUES (?)",
                    [(os.urandom(200).hex(),) for _ in range(rows)])
    con.commit()
    con.close()
    return path


def _pages(path):
    data = path.read_bytes()
    ps = int.from_bytes(data[16:18], "big") or 65536
    return ps, [data[i:i + ps] for i in range(0, len(data), ps)]


def _db50(tmp_path):
    p = tmp_path / "s.sqlite3"
    n = 0
    rows = 100
    while n != 50:
        p.unlink(missing_ok=True)
        _make_db(p, rows)
        n = len(_pages(p)[1])
        rows += (50 - n) // 2 or (1 if n < 50 else -1)
    return p


def _rewrite(path, pgno):
    ps, _ = _pages(path)
    with path.open("r+b") as f:
        f.seek((pgno - 1) * ps + 100)
        f.write(b"\xAB" * 16)


def test_scan_on_empty_index_reports_every_page(tmp_path):
    db = _db50(tmp_path)
    res = bp.scan_changes(db, bp.PageIndex())
    assert res.page_count == 50
    assert res.changed == list(range(1, 51))
    assert res.page_size == 4096


def test_scan_reports_only_rewritten_pages(tmp_path):
    db = _db50(tmp_path)
    idx = bp.scan_changes(db, bp.PageIndex()).new_index
    _rewrite(db, 7)
    _rewrite(db, 30)
    assert bp.scan_changes(db, idx).changed == [7, 30]


def test_scan_handles_shrink_and_growth(tmp_path):
    db = _db50(tmp_path)
    idx = bp.scan_changes(db, bp.PageIndex()).new_index
    ps = 4096
    with db.open("ab") as f:
        f.write(b"\x01" * ps * 3)
    grown = bp.scan_changes(db, idx)
    assert grown.page_count == 53 and len(grown.new_index) == 53
    assert grown.changed == [51, 52, 53]
    with db.open("r+b") as f:
        f.truncate(ps * 40)
    shrunk = bp.scan_changes(db, grown.new_index)
    assert shrunk.page_count == 40 and len(shrunk.new_index) == 40
    assert shrunk.changed == []


def test_db_sha256_matches_hashlib(tmp_path):
    db = _db50(tmp_path)
    res = bp.scan_changes(db, bp.PageIndex())
    assert res.db_sha256 == hashlib.sha256(db.read_bytes()).hexdigest()


def test_page_size_one_means_65536(tmp_path):
    db = tmp_path / "big.sqlite3"
    _make_db(db, rows=300, page_size=65536)
    res = bp.scan_changes(db, bp.PageIndex())
    assert res.page_size == 65536
    assert res.page_count == db.stat().st_size // 65536


def test_segments_round_trip_pages(tmp_path):
    db = _db50(tmp_path)
    key = backup.generate_escrow_key()
    segs = bp.write_segments(db, list(range(1, 51)), out_dir=tmp_path / "o",
                             prefix="base", key=key, header={"page_size": 4096})
    assert len(segs) == 1 and segs[0].name == "base-0001"
    assert segs[0].pages == 50 and (segs[0].first_pgno, segs[0].last_pgno) == (1, 50)
    assert segs[0].sha256 == hashlib.sha256(segs[0].path.read_bytes()).hexdigest()
    assert segs[0].md5 == hashlib.md5(segs[0].path.read_bytes()).hexdigest()
    assert segs[0].size == segs[0].path.stat().st_size
    assert not list((tmp_path / "o").glob("*.tmp"))
    _, expect = _pages(db)
    header, it = bp.read_segment(segs[0].path, key)
    assert header["page_size"] == 4096 and header["first_pgno"] == 1
    got = dict(it)
    assert sorted(got) == list(range(1, 51))
    assert all(got[n] == expect[n - 1] for n in got)


def test_segments_split_at_max_bytes(tmp_path, monkeypatch):
    db = _make_db(tmp_path / "s.sqlite3", rows=600)
    monkeypatch.setattr(bp, "SEGMENT_MAX_BYTES", 20_000)
    key = backup.generate_escrow_key()
    n = len(_pages(db)[1])
    segs = bp.write_segments(db, list(range(1, n + 1)), out_dir=tmp_path / "o",
                             prefix="delta", key=key, header={})
    assert len(segs) > 1
    assert [s.name for s in segs][:2] == ["delta-0001", "delta-0002"]
    assert segs[0].first_pgno == 1 and segs[-1].last_pgno == n
    for a, b in zip(segs, segs[1:]):
        assert b.first_pgno == a.last_pgno + 1
    seen = []
    for s in segs:
        seen += [p for p, _ in bp.read_segment(s.path, key)[1]]
    assert seen == list(range(1, n + 1))


def test_segment_wrong_key_raises(tmp_path):
    db = _db50(tmp_path)
    segs = bp.write_segments(db, [1, 2], out_dir=tmp_path / "o", prefix="b",
                             key=backup.generate_escrow_key(), header={})
    with pytest.raises(InvalidToken):
        bp.read_segment(segs[0].path, backup.generate_escrow_key())


def test_segment_bad_magic_raises(tmp_path):
    key = backup.generate_escrow_key()
    p = tmp_path / "bad"
    with backup.open_encrypted(p, key) as f:
        f.write(b"NOT-A-SEGMENT\n{}\n")
    with pytest.raises(ValueError):
        bp.read_segment(p, key)


def test_index_round_trip_and_truncate(tmp_path):
    idx = bp.PageIndex()
    idx.set(1, bp.page_digest(b"a"))
    idx.set(2, bp.page_digest(b"b"))
    idx.save(tmp_path / "i.bin")
    back = bp.PageIndex.load(tmp_path / "i.bin")
    assert len(back) == 2 and back.get(2) == bp.page_digest(b"b")
    assert back.get(3) is None
    back.truncate(1)
    assert len(back) == 1
    assert len(bp.PageIndex.load(tmp_path / "missing")) == 0


def test_index_save_is_atomic(tmp_path, monkeypatch):
    path = tmp_path / "i.bin"
    old = bp.PageIndex()
    old.set(1, bp.page_digest(b"old"))
    old.save(path)
    new = bp.PageIndex()
    new.set(1, bp.page_digest(b"new"))
    new.set(2, bp.page_digest(b"new2"))

    def boom(*a, **k):
        raise OSError("nope")
    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        new.save(path)
    monkeypatch.undo()
    assert bp.PageIndex.load(path).get(1) == bp.page_digest(b"old")
    assert len(bp.PageIndex.load(path)) == 1


def test_records_fingerprint_changes_with_content_not_mtime(tmp_path):
    rec = tmp_path / "rec"
    (rec / "sub").mkdir(parents=True)
    f = rec / "sub" / "a.md"
    f.write_text("one")
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    fp = bp.records_fingerprint(rec, cfg)
    os.utime(f, (1, 1))
    assert bp.records_fingerprint(rec, cfg) == fp
    f.write_text("two")
    assert bp.records_fingerprint(rec, cfg) != fp


def test_records_tar_uses_restore_arc_names(tmp_path):
    rec = tmp_path / "rec"
    rec.mkdir()
    (rec / "a.md").write_text("hello")
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    key = backup.generate_escrow_key()
    out = tmp_path / "records-000001"
    sha, size = bp.records_tar(rec, cfg, out, key)
    assert size == out.stat().st_size
    assert sha == hashlib.sha256(out.read_bytes()).hexdigest()
    plain = backup.decrypt_file(out, tmp_path / "plain.tgz", key)
    with tarfile.open(plain) as t:
        names = t.getnames()
    assert "records/a.md" in names and "config.json" in names


def _zero_db(path, npages=64, ps=4096):
    data = bytearray(npages * ps)
    data[:16] = b"SQLite format 3\x00"
    data[16:18] = ps.to_bytes(2, "big")
    path.write_bytes(bytes(data))
    return path


def test_read_segment_bounded_memory_on_sparse_pages(tmp_path, monkeypatch):
    db = _zero_db(tmp_path / "z.sqlite3")
    key = backup.generate_escrow_key()
    segs = bp.write_segments(db, list(range(1, 65)), out_dir=tmp_path / "o",
                             prefix="z", key=key, header={})
    monkeypatch.setattr(bp, "_MAX_OUT", 8192)
    _, it = bp.read_segment(segs[0].path, key)
    got = list(it)
    assert [n for n, _ in got] == list(range(1, 65))
    assert all(p == db.read_bytes()[(n - 1) * 4096:n * 4096] for n, p in got)
    assert it.peak_buffered <= 8192 + 4 + 4096


def test_write_segments_rejects_short_page_and_bad_pgno(tmp_path):
    db = _zero_db(tmp_path / "z.sqlite3", npages=4)
    with db.open("ab") as f:
        f.write(b"\x00" * 100)  # ragged tail
    key = backup.generate_escrow_key()
    with pytest.raises(ValueError):
        bp.write_segments(db, [5], out_dir=tmp_path / "o", prefix="a", key=key, header={})
    with pytest.raises(ValueError):
        bp.write_segments(db, [9], out_dir=tmp_path / "o", prefix="a", key=key, header={})
    with pytest.raises(ValueError):
        bp.write_segments(db, [0], out_dir=tmp_path / "o", prefix="a", key=key, header={})


def test_read_segment_header_only_leaves_no_plaintext(tmp_path, monkeypatch):
    import gc
    import tempfile
    made = []
    real = tempfile.mkdtemp

    def spy(*a, **k):
        d = real(*a, **k)
        made.append(d)
        return d
    monkeypatch.setattr(tempfile, "mkdtemp", spy)
    db = _db50(tmp_path)
    key = backup.generate_escrow_key()
    segs = bp.write_segments(db, [1, 2], out_dir=tmp_path / "o", prefix="b",
                             key=key, header={})
    header, it = bp.read_segment(segs[0].path, key)
    seg_dirs = [d for d in made if "mcpbrain-seg-" in d]
    assert seg_dirs and all(os.path.exists(d) for d in seg_dirs)
    del it
    gc.collect()
    assert header["pages"] == 2
    assert not any(os.path.exists(d) for d in seg_dirs)


def _hand_segment(path, key, body, header):
    with backup.open_encrypted(path, key) as enc:
        enc.write(bp._SEGMENT_MAGIC)
        enc.write(json.dumps(header).encode() + b"\n")
        enc.write(body)


def test_read_segment_detects_truncated_body(tmp_path):
    key = backup.generate_escrow_key()
    ps = 4096
    recs = b"".join(bp._PGNO.pack(n) + bytes([n]) * ps for n in (1, 2, 3))
    hdr = {"page_size": ps, "pages": 3, "first_pgno": 1, "last_pgno": 3}
    full = zlib.compress(recs)
    # cut mid-stream
    _hand_segment(tmp_path / "cut", key, full[:-6], hdr)
    with pytest.raises(ValueError):
        list(bp.read_segment(tmp_path / "cut", key)[1])
    # clean stream cut at a record boundary: eof ok, page count short
    _hand_segment(tmp_path / "short", key, zlib.compress(recs[: 2 * (4 + ps)]), hdr)
    with pytest.raises(ValueError):
        list(bp.read_segment(tmp_path / "short", key)[1])
    # intact passes
    _hand_segment(tmp_path / "ok", key, full, hdr)
    assert len(list(bp.read_segment(tmp_path / "ok", key)[1])) == 3
