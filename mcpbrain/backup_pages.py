"""Page-level change tracking and encrypted page segments for incremental backup.

The store is one multi-GB SQLite file; re-uploading it daily is what made the
old backup slow and fragile. SQLite's file is an array of fixed-size pages, so a
capture can hash every page, compare against the digests it recorded last time
(`PageIndex`), and ship only the pages that changed. This module is pure local
logic: hashing, packing changed pages into encrypted segment files, reading them
back, and packaging the records repo + config.json. It does no network I/O and
no scheduling — `backup_capture` and `backup_restore` drive it.

The page unit is the store's own `page_size` (read from the header), never a
fixed block size, so a rebuild that changes page_size is visible to the caller
as a different `ScanResult.page_size` and can force a fresh base.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import struct
import tarfile
import tempfile
import zlib
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

from mcpbrain import backup

# Compressed payload ceiling for one segment: bounds memory on both sides and
# keeps each Drive upload a small, individually resumable file.
SEGMENT_MAX_BYTES = 16 * 1024 * 1024

_DIGEST_SIZE = 16
_SEGMENT_MAGIC = b"MCPBRAIN-PAGES-1\n"
_PGNO = struct.Struct(">I")
_ZLIB_LEVEL = 6
# zlib buffers internally, so its output length lags the input. Closing a
# segment a little early keeps the final (flushed) size under the ceiling.
_FLUSH_SLACK = 128 * 1024


def page_digest(page: bytes) -> bytes:
    return hashlib.blake2b(page, digest_size=_DIGEST_SIZE).digest()


class PageIndex:
    """Per-page digests of the last captured state, page 1 first.

    Stored as concatenated 16-byte digests: 8 KiB pages on a 12 GB store is
    ~1.5M pages, so the index is ~24 MB — cheap to keep and rewrite.
    """

    def __init__(self, digests: bytes | bytearray = b""):
        self._d = bytearray(digests)

    @classmethod
    def load(cls, path) -> "PageIndex":
        try:
            data = Path(path).read_bytes()
        except FileNotFoundError:
            return cls()
        # A torn trailing digest can only mean a damaged file; drop the stub so
        # the pages it described simply read as changed.
        return cls(data[: len(data) - len(data) % _DIGEST_SIZE])

    def save(self, path) -> None:
        """Temp + os.replace: a crash leaves the previous index intact."""
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_bytes(bytes(self._d))
        os.replace(tmp, path)

    def __len__(self) -> int:
        return len(self._d) // _DIGEST_SIZE

    def get(self, pgno: int) -> bytes | None:
        if pgno < 1 or pgno > len(self):
            return None
        off = (pgno - 1) * _DIGEST_SIZE
        return bytes(self._d[off:off + _DIGEST_SIZE])

    def set(self, pgno: int, digest: bytes) -> None:
        if len(digest) != _DIGEST_SIZE:
            raise ValueError("digest must be 16 bytes")
        need = pgno * _DIGEST_SIZE
        if len(self._d) < need:
            self._d.extend(b"\x00" * (need - len(self._d)))
        off = (pgno - 1) * _DIGEST_SIZE
        self._d[off:off + _DIGEST_SIZE] = digest

    def truncate(self, page_count: int) -> None:
        del self._d[page_count * _DIGEST_SIZE:]


@dataclass
class ScanResult:
    page_size: int
    page_count: int
    changed: list[int]
    db_sha256: str
    new_index: PageIndex


def _page_size(header: bytes) -> int:
    if len(header) < 18 or not header.startswith(b"SQLite format 3\x00"):
        raise ValueError("not a SQLite database")
    ps = int.from_bytes(header[16:18], "big")
    return 65536 if ps == 1 else ps


def scan_changes(db_path, index: PageIndex) -> ScanResult:
    """Hash every page in one pass; report pages whose digest differs from index.

    Pages past the end of the old index are changed by definition; pages the
    file no longer has are dropped from `new_index` (the caller sees the shrink
    through `page_count`). The whole-file sha256 rides the same read.
    """
    new = PageIndex()
    changed: list[int] = []
    whole = hashlib.sha256()
    with open(db_path, "rb") as f:
        head = f.read(100)
        ps = _page_size(head)
        f.seek(0)
        pgno = 0
        while True:
            page = f.read(ps)
            if not page:
                break
            pgno += 1
            whole.update(page)
            dg = page_digest(page)
            new.set(pgno, dg)
            if index.get(pgno) != dg:
                changed.append(pgno)
    return ScanResult(page_size=ps, page_count=pgno, changed=changed,
                      db_sha256=whole.hexdigest(), new_index=new)


@dataclass
class SegmentFile:
    name: str
    path: Path
    size: int
    sha256: str
    md5: str
    first_pgno: int
    last_pgno: int
    pages: int


def _finish_segment(buf: bytes, pgnos: list[int], *, out_dir: Path, name: str,
                    key: bytes, header: dict) -> SegmentFile:
    hdr = dict(header, first_pgno=pgnos[0], last_pgno=pgnos[-1])
    path = out_dir / name
    tmp = out_dir / (name + ".tmp")
    with backup.open_encrypted(tmp, key) as enc:
        enc.write(_SEGMENT_MAGIC)
        enc.write(json.dumps(hdr, sort_keys=True).encode() + b"\n")
        enc.write(buf)
    os.replace(tmp, path)
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            sha.update(blk)
            md5.update(blk)
    return SegmentFile(name=name, path=path, size=path.stat().st_size,
                       sha256=sha.hexdigest(), md5=md5.hexdigest(),
                       first_pgno=pgnos[0], last_pgno=pgnos[-1], pages=len(pgnos))


def write_segments(db_path, pgnos, *, out_dir, prefix, key: bytes,
                   header: dict) -> list[SegmentFile]:
    """Pack the given pages into encrypted segment files `<prefix>-0001`, ...

    Payload: magic line, one JSON header line (caller's header plus
    first/last pgno), then a zlib stream of `>I` pgno + page records. The header
    carries the range, which is only known once the segment is full, so each
    segment's compressed body is held in memory (<=16 MiB) and written out in
    one go. Files go to `<name>.tmp` then rename, so a crash never leaves a
    plausible-looking partial segment.
    """
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    limit = max(SEGMENT_MAX_BYTES - min(_FLUSH_SLACK, SEGMENT_MAX_BYTES // 4), 1)
    segments: list[SegmentFile] = []

    with open(db_path, "rb") as f:
        ps = _page_size(f.read(100))
        # Stamped here (not left to the caller) so a segment is self-describing.
        header = dict(header, page_size=ps)
        comp = zlib.compressobj(_ZLIB_LEVEL)
        parts: list[bytes] = []
        size = 0
        cur: list[int] = []

        def close() -> None:
            nonlocal comp, parts, size, cur
            parts.append(comp.flush())
            name = f"{prefix}-{len(segments) + 1:04d}"
            segments.append(_finish_segment(b"".join(parts), cur, out_dir=out_dir,
                                            name=name, key=key, header=header))
            comp, parts, size, cur = zlib.compressobj(_ZLIB_LEVEL), [], 0, []

        for pgno in pgnos:
            f.seek((pgno - 1) * ps)
            page = f.read(ps)
            out = comp.compress(_PGNO.pack(pgno) + page)
            if out:
                parts.append(out)
                size += len(out)
            cur.append(pgno)
            if size >= limit:
                close()
        if cur:
            close()
    return segments


def read_segment(path, key: bytes) -> tuple[dict, Iterator[tuple[int, bytes]]]:
    """Return (header, iterator of (pgno, page)). ValueError on a bad magic.

    Decrypts to a private temp file, which the iterator removes when it is
    exhausted or closed; the header is parsed eagerly so a wrong file fails here
    rather than part-way through a restore. A wrong key raises InvalidToken.
    """
    tmpdir = Path(tempfile.mkdtemp(prefix="mcpbrain-seg-"))
    try:
        plain = backup.decrypt_file(path, tmpdir / "seg", key)
        fh = plain.open("rb")
        if fh.read(len(_SEGMENT_MAGIC)) != _SEGMENT_MAGIC:
            fh.close()
            raise ValueError("not an mcpbrain page segment")
        header = json.loads(fh.readline())
        page_size = int(header["page_size"])  # always stamped by write_segments
    except BaseException:
        shutil.rmtree(tmpdir, ignore_errors=True)
        raise

    def pages() -> Iterator[tuple[int, bytes]]:
        try:
            dec = zlib.decompressobj()
            buf = b""
            ps = page_size
            while True:
                blk = fh.read(1 << 20)
                if blk:
                    buf += dec.decompress(blk)
                else:
                    buf += dec.flush()
                while ps and len(buf) >= 4 + ps:
                    yield _PGNO.unpack_from(buf)[0], buf[4:4 + ps]
                    buf = buf[4 + ps:]
                if not blk:
                    break
            if buf:
                raise ValueError("truncated page record in segment")
        finally:
            fh.close()
            shutil.rmtree(tmpdir, ignore_errors=True)

    return header, pages()


def _record_files(records_dir, config_path):
    """Yield (arcname, path) for every file, sorted — same set records_tar packs."""
    records_dir = Path(records_dir) if records_dir else None
    if records_dir and records_dir.exists():
        found = []
        for root, _dirs, files in os.walk(records_dir):
            for fn in files:
                p = Path(root) / fn
                rel = p.relative_to(records_dir).as_posix()
                found.append((f"{backup._RECORDS_ARC}/{rel}", p))
        yield from sorted(found)
    if config_path and Path(config_path).exists():
        yield backup._CONFIG_ARC, Path(config_path)


def records_fingerprint(records_dir, config_path) -> str:
    """sha256 over sorted (relpath, size, sha256) of every file.

    Content only — mtimes churn on a git checkout without any change, and a
    fingerprint that moved on every look would re-upload the repo for nothing.
    """
    outer = hashlib.sha256()
    for arc, p in _record_files(records_dir, config_path):
        h = hashlib.sha256()
        with p.open("rb") as f:
            for blk in iter(lambda: f.read(1 << 20), b""):
                h.update(blk)
        outer.update(json.dumps([arc, p.stat().st_size, h.hexdigest()]).encode())
        outer.update(b"\n")
    return outer.hexdigest()


def records_tar(records_dir, config_path, out_path, key: bytes) -> tuple[str, int]:
    """Encrypted tar.gz of the records repo + config.json -> (sha256, size).

    Arc names are `backup._RECORDS_ARC` / `_CONFIG_ARC`, so the existing restore
    extraction understands it. The tar streams straight into the encryptor (no
    cleartext copy on disk). sha256 is of the CIPHERTEXT — what lands on Drive.
    """
    out_path = Path(out_path)
    with backup.open_encrypted(out_path, key) as enc:
        with tarfile.open(fileobj=enc, mode="w|gz") as tar:
            if records_dir and Path(records_dir).exists():
                tar.add(records_dir, arcname=backup._RECORDS_ARC)
            if config_path and Path(config_path).exists():
                tar.add(config_path, arcname=backup._CONFIG_ARC)
    h = hashlib.sha256()
    with out_path.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            h.update(blk)
    return h.hexdigest(), out_path.stat().st_size
