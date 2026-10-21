"""Restore the newest restore point across every backup location.

Restore points come from two layouts (spec §1, §5):

* **Manifests** in the incremental layout, ``<person>/<chain>/manifest-<chain>-
  <seq:06d>``: an encrypted JSON naming a base, every delta up to that seq and
  the newest records file. ``captured_at`` is read from the manifest.
* **Legacy** single-file ``.enc`` snapshots, in every legacy folder and loose in
  the person folder. ``captured_at`` is the file's Drive ``createdTime``.

They are tried strictly newest first, by capture time, across ALL of them: a
location is never preferred over a newer backup, so the June legacy file never
beats a newer manifest, and the previous manifest of a chain comes before an
older chain. Discovery stays cheap: manifest NAMES carry the seq, so only each
chain's newest manifest is decrypted to rank chains; an older one is decrypted
only when restore falls back to it. Decrypted manifests are cached by file id.

**Never damages the live store.** Pages are written into ``<dest>.stage``,
which must match the manifest's ``db_sha256`` and pass ``PRAGMA
integrity_check`` before ``swap(stage, dest)`` (default
``backup.swap_in_store``) runs; the swap is the only thing that touches
``dest``. Records + config are decrypted into a private work dir first and
placed only after the swap.

**Fallback.** A candidate that is provably broken -- a file missing on Drive
(404/410), a ciphertext sha256 mismatch, anything failing to decrypt or parse,
a db_sha256 mismatch, a failed integrity check -- is recorded in
``RestoreResult.fallbacks`` with its reason, logged, and the next older one is
tried. A TRANSIENT download failure (network, 5xx, 429...) is not evidence
against the restore point and propagates instead: demoting to an older point
because of a dropped link would silently restore stale data. ``NoRestorePoint``
when every candidate fails.

**Resume.** Files download to ``<backup dir>/restore/<file_id>`` and are kept
until the restore succeeds; a file already there with its recorded sha256 is
not downloaded again. ``budget_s`` running out raises ``RestoreIncomplete``,
leaving what is verified for the next call.
"""
from __future__ import annotations

import errno
import gzip
import hashlib
import heapq
import json
import logging
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterator, Literal

from mcpbrain import backup, backup_location, backup_outbox as bo, backup_pages
from mcpbrain import backup_prune, backup_uploader as bu

log = logging.getLogger(__name__)

_NUM_RETRIES = backup._NUM_RETRIES
_MANIFEST_RE = re.compile(r"^manifest-(chain-[0-9]{8}-[0-9a-f]+)-([0-9]{6,})$")
_MISSING = (404, 410)


@dataclass(frozen=True)
class Candidate:
    kind: Literal["manifest", "legacy"]
    captured_at: float
    file_id: str
    folder_id: str
    label: str


@dataclass
class RestoreResult:
    used: Candidate
    fallbacks: list[tuple[Candidate, str]]
    path: Path


class NoRestorePoint(Exception):
    """No candidate could be restored; `fallbacks` says why for each."""

    def __init__(self, msg: str, fallbacks: list[tuple[Candidate, str]] | None = None):
        super().__init__(msg)
        self.fallbacks = list(fallbacks or [])


class RestoreIncomplete(Exception):
    """`budget_s` ran out mid-download; verified files stay for the next call."""


class ListingIncomplete(TimeoutError):
    """Drive flagged a listing `incompleteSearch`. Transient (a TimeoutError,
    as RestoreBusyError is): retry later, never restore from a partial view."""


class _Bad(Exception):
    """This candidate is provably unusable: fall back to the next one."""


class _Transient(Exception):
    """A download failed for a reason that says nothing about the candidate."""


def restore_dir(home) -> Path:
    return bo.backup_dir(home) / "restore"


# ---------------------------------------------------------------- discovery

@dataclass
class _Chain:
    name: str
    folder_id: str
    # (seq, [file ids]) newest first; duplicates of one seq share an entry (R12)
    manifests: list[tuple[int, list[str]]] = field(default_factory=list)
    pos: int = 0


def _files(svc, parent: str, *, drive_id: str | None, extra: str = "") -> list[dict]:
    """Children of `parent`. A listing Drive marks `incompleteSearch` may omit
    the newest chain or manifest, so restoring from it could silently pick an
    older point: raise ListingIncomplete (transient) instead."""
    files, complete = backup_prune._list(
        svc, f"'{parent}' in parents and trashed = false{extra}", drive_id=drive_id)
    if not complete:
        raise ListingIncomplete(
            f"Drive returned an incomplete listing for folder {parent}; "
            "restore will retry rather than risk choosing an older point")
    return files


def _legacy_from(files: list[dict], folder_id: str) -> list[Candidate]:
    out = []
    for f in files:
        if f.get("mimeType") == backup.FOLDER_MIME:
            continue
        name = str(f.get("name", ""))
        if not name.endswith(".enc"):
            continue
        created = backup_prune._ts(f.get("createdTime"))
        if created is None:
            log.warning("backup restore: legacy file %s (%s) has no createdTime; "
                        "it cannot be ranked and is ignored", name, f.get("id"))
            continue
        out.append(Candidate("legacy", created, f["id"], folder_id, name))
    return out


def _discover(home, svc) -> tuple[list[_Chain], list[Candidate]]:
    loc = backup_location.resolve(home, svc, create=False)
    chains: list[_Chain] = []
    legacy: list[Candidate] = []
    if loc.folder_id:
        kids = _files(svc, loc.folder_id, drive_id=loc.drive_id)
        legacy += _legacy_from(kids, loc.folder_id)
        for f in sorted(kids, key=lambda f: f["id"]):
            if (f.get("mimeType") == backup.FOLDER_MIME
                    and str(f.get("name", "")).startswith("chain-")):
                chains.append(_chain(svc, loc, f))
    for folder in loc.legacy_folders:
        legacy += _legacy_from(
            _files(svc, folder, drive_id=None,
                   extra=f" and mimeType != '{backup.FOLDER_MIME}'"), folder)
    return [c for c in chains if c.manifests], legacy


def _chain(svc, loc, folder: dict) -> _Chain:
    by_seq: dict[int, list[str]] = {}
    for f in _files(svc, folder["id"], drive_id=loc.drive_id,
                    extra=" and name contains 'manifest-'"):
        m = _MANIFEST_RE.match(str(f.get("name", "")))
        if not m or f.get("mimeType") == backup.FOLDER_MIME:
            continue
        if m.group(1) != folder["name"]:
            log.warning("backup restore: %s sits in chain folder %s; ignored",
                        f.get("name"), folder["name"])
            continue
        by_seq.setdefault(int(m.group(2)), []).append(f["id"])
    return _Chain(folder["name"], folder["id"],
                  [(s, sorted(ids)) for s, ids in sorted(by_seq.items(), reverse=True)])


def _label(chain: str, seq: int) -> str:
    return f"{chain} seq {seq}"


def _manifest(svc, key, home, chain: _Chain, seq: int, file_id: str,
              deadline, clock) -> dict:
    """The decrypted, validated manifest (cached by file id). _Bad if unusable."""
    cache = restore_dir(home) / "manifests" / f"{file_id}.json"
    try:
        m = json.loads(cache.read_text())
    except (OSError, ValueError):
        m = None
    if m is None:
        enc = restore_dir(home) / "manifests" / f"{file_id}.enc"
        try:
            _download(svc, file_id, enc, name=_label(chain.name, seq),
                      deadline=deadline, clock=clock)
            plain = enc.with_suffix(".plain")
            try:
                backup.decrypt_file(enc, plain, key)
                m = json.loads(plain.read_text())
            finally:
                plain.unlink(missing_ok=True)
        except (_Bad, _Transient, RestoreIncomplete):
            raise
        except OSError as exc:
            if _is_local_env_error(exc):
                raise      # e.g. ENOSPC writing .plain: this disk, not the manifest
            raise _Bad(f"manifest unreadable: {type(exc).__name__}: {exc}") from exc
        except Exception as exc:  # noqa: BLE001 — undecryptable = broken candidate
            raise _Bad(f"manifest unreadable: {type(exc).__name__}: {exc}") from exc
        finally:
            enc.unlink(missing_ok=True)
        _check_manifest(m, chain.name, seq)
        bo._write_atomic(cache, json.dumps(m, sort_keys=True).encode())
    _check_manifest(m, chain.name, seq)
    return m


def _check_manifest(m, chain: str, seq: int) -> None:
    try:
        ok = (isinstance(m, dict) and m.get("format") == 1
              and m.get("chain") == chain and int(m.get("seq")) == seq
              and int(m.get("page_size")) > 0 and int(m.get("page_count")) > 0
              and isinstance(m.get("db_sha256"), str)
              and isinstance(m.get("captured_at"), (int, float))
              and isinstance(m.get("files"), list) and m["files"]
              and all(isinstance(f, dict) and {"name", "file_id", "size", "sha256",
                                               "kind"} <= set(f) for f in m["files"])
              and m["files"][0]["kind"] == "base")
    except (TypeError, ValueError):
        ok = False
    if not ok:
        raise _Bad("manifest unreadable: malformed or not for this chain/seq")


def _head(svc, key, home, chain: _Chain, deadline, clock,
          bad: list[tuple[Candidate, str]]):
    """Decrypt the chain's next manifest from `pos`: (Candidate, manifest), or
    None when the chain is exhausted. Unreadable ones are appended to `bad`."""
    while chain.pos < len(chain.manifests):
        seq, ids = chain.manifests[chain.pos]
        chain.pos += 1
        why = None
        for fid in ids:
            try:
                m = _manifest(svc, key, home, chain, seq, fid, deadline, clock)
            except _Bad as exc:
                why = str(exc)
                continue
            return (Candidate("manifest", float(m["captured_at"]), fid,
                              chain.folder_id, _label(chain.name, seq)), m)
        bad.append((Candidate("manifest", 0.0, ids[0], chain.folder_id,
                              _label(chain.name, seq)), why or "manifest unreadable"))
    return None


def _ordered(home, svc, key, *, deadline=None, clock=time.monotonic,
             bad: list | None = None) -> Iterator[tuple[Candidate, dict | None]]:
    """Every candidate, strictly newest first, decrypting a chain's older
    manifests only when the caller asks for the next one."""
    bad = bad if bad is not None else []
    chains, legacy = _discover(home, svc)
    heap: list = []
    n = 0

    def push(cand, manifest, chain):
        nonlocal n
        n += 1
        # newest first; on a tie a manifest before a legacy file
        heapq.heappush(heap, (-cand.captured_at, cand.kind != "manifest", n,
                              cand, manifest, chain))

    for c in legacy:
        push(c, None, None)
    for ch in chains:
        h = _head(svc, key, home, ch, deadline, clock, bad)
        if h:
            push(*h, ch)
    while heap:
        _neg, _k, _n, cand, manifest, chain = heapq.heappop(heap)
        yield cand, manifest
        if chain is not None:
            h = _head(svc, key, home, chain, deadline, clock, bad)
            if h:
                push(*h, chain)


def candidates(home, drive_service, key) -> list[Candidate]:
    """Restore points, newest first: each chain's newest READABLE manifest and
    every legacy file. Older manifests of a chain are reached only as
    fallbacks inside `restore_latest` (decrypting all of them here would cost
    one download per hourly capture)."""
    chains, legacy = _discover(home, drive_service)
    out = list(legacy)
    bad: list = []
    for ch in chains:
        try:
            h = _head(drive_service, key, home, ch, None, time.monotonic, bad)
        except _Transient as exc:
            raise exc.__cause__ from None
        if h:
            out.append(h[0])
    for cand, why in bad:
        log.warning("backup restore: skipping %s: %s", cand.label, why)
    out.sort(key=lambda c: (c.captured_at, c.kind == "manifest"), reverse=True)
    return out


# ---------------------------------------------------------------- download

def _sha256_file(path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _over(deadline, clock) -> bool:
    return deadline is not None and clock() >= deadline


def _download(svc, file_id: str, dest: Path, *, name: str, deadline, clock) -> None:
    """Download to `dest` via `<dest>.part`. 404/410 -> _Bad; any other failure
    -> _Transient; budget -> RestoreIncomplete (partial file removed)."""
    if _over(deadline, clock):
        raise RestoreIncomplete(f"restore budget ran out before {name}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    part = dest.with_name(dest.name + ".part")
    try:
        request = svc.files().get_media(fileId=file_id, supportsAllDrives=True)
        with open(part, "wb") as fh:
            downloader = backup._default_downloader(fh, request)
            done = False
            while not done:
                _status, done = downloader.next_chunk(num_retries=_NUM_RETRIES)
                if not done and _over(deadline, clock):
                    raise RestoreIncomplete(f"restore budget ran out during {name}")
        os.replace(part, dest)
    except RestoreIncomplete:
        part.unlink(missing_ok=True)
        raise
    except Exception as exc:  # noqa: BLE001 — classified below
        part.unlink(missing_ok=True)
        if bu.http_status(exc) in _MISSING:
            raise _Bad(f"{name} ({file_id}) missing on Drive") from exc
        raise _Transient(str(exc)) from exc


def _fetch(svc, home, spec: dict, *, deadline, clock) -> Path:
    """The verified local copy of a manifest-named file (the resume point)."""
    dest = restore_dir(home) / spec["file_id"]
    if dest.is_file():
        if dest.stat().st_size == int(spec["size"]) and _sha256_file(dest) == spec["sha256"]:
            return dest
        dest.unlink()
    _download(svc, spec["file_id"], dest, name=spec["name"], deadline=deadline, clock=clock)
    if dest.stat().st_size != int(spec["size"]) or _sha256_file(dest) != spec["sha256"]:
        dest.unlink(missing_ok=True)
        raise _Bad(f"{spec['name']} ({spec['file_id']}) does not match its recorded "
                   "sha256/size")
    return dest


# ---------------------------------------------------------------- verify + place

# sqlite messages that mean THIS machine's trouble (SQLITE_IOERR, CANTOPEN,
# FULL), not a corrupt restore point ("malformed", "not a database").
_SQLITE_LOCAL = (("database or disk is full", errno.ENOSPC),
                 ("disk i/o error", errno.EIO),
                 ("unable to open database file", errno.EIO))


def _sqlite_local_errno(exc: BaseException) -> int | None:
    if not isinstance(exc, sqlite3.OperationalError):
        return None
    msg = str(exc).lower()
    for text, code in _SQLITE_LOCAL:
        if text in msg:
            return code
    return None


def _integrity(path: Path) -> None:
    if not path.is_file() or path.stat().st_size == 0:
        raise _Bad("restore point carried no store")
    try:
        con = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro&immutable=1",
                              uri=True)
        try:
            rows = [r[0] for r in con.execute("PRAGMA integrity_check").fetchall()]
        finally:
            con.close()
    except sqlite3.Error as exc:
        local = _sqlite_local_errno(exc)
        if local is not None:
            # This machine's disk, not the restore point: never fall back on it.
            raise OSError(local, f"could not check the restored store: {exc}") from exc
        raise _Bad(f"integrity_check failed: {exc}") from exc
    if rows != ["ok"]:
        raise _Bad(f"integrity_check failed: {'; '.join(map(str, rows[:5]))}")


def _remove_stage(stage: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(stage) + suffix).unlink(missing_ok=True)


def _build_from_manifest(svc, key, home, m: dict, stage: Path, work: Path,
                         want_records: bool, *, deadline, clock) -> None:
    ps, count = int(m["page_size"]), int(m["page_count"])
    pages = [f for f in m["files"] if f["kind"] != "records"]
    records = [f for f in m["files"] if f["kind"] == "records"][-1:]
    # Download (and so verify) everything first: the resume point, and no
    # decryption work is wasted on a candidate missing a file.
    local = [(f, _fetch(svc, home, f, deadline=deadline, clock=clock))
             for f in pages + (records if want_records else [])]
    with _open_stage(stage) as out:
        for spec, path in local[:len(pages)]:
            header, it = backup_pages.read_segment(path, key)
            with it:
                if int(header.get("page_size", 0)) != ps:
                    raise _Bad(f"{spec['name']} has page_size "
                               f"{header.get('page_size')}, manifest says {ps}")
                for pgno, page in it:
                    if 1 <= pgno <= count:
                        out.seek((pgno - 1) * ps)
                        out.write(page)
        out.truncate(count * ps)
    if _sha256_file(stage) != m["db_sha256"]:
        raise _Bad("rebuilt store does not match the manifest's db_sha256")
    _integrity(stage)
    if want_records and len(local) > len(pages):
        backup.restore(local[-1][1], work / "no-store.sqlite3", key,
                       records_dir=work / "records", config_path=work / "config.json")


def _build_from_legacy(svc, key, home, cand: Candidate, stage: Path, work: Path,
                       want_records: bool, *, deadline, clock) -> None:
    enc = restore_dir(home) / cand.file_id
    enc.unlink(missing_ok=True)   # no recorded sha256 to resume against
    _download(svc, cand.file_id, enc, name=cand.label, deadline=deadline, clock=clock)
    backup.restore(enc, stage, key,
                   records_dir=(work / "records") if want_records else None,
                   config_path=(work / "config.json") if want_records else None)
    _integrity(stage)


def _open_stage(stage: Path):
    return open(stage, "wb")


def _is_local_env_error(exc: BaseException) -> bool:
    """An OSError from THIS machine (ENOSPC, EDQUOT, EIO, EROFS, EACCES...)
    says nothing about the restore point: falling back on it would restore an
    older, smaller point because the disk is full. gzip's BadGzipFile is an
    OSError too, but it means the artifact's content is malformed."""
    return isinstance(exc, OSError) and not isinstance(exc, gzip.BadGzipFile)


def _place_records(work: Path, records_dir, config_path) -> None:
    """Replace records_dir / config_path atomically: build the new copy beside
    the destination, then rename the old aside and the new in. Any failure
    leaves the original in place."""
    rec = work / "records"
    if records_dir and rec.is_dir():
        _replace_dir(rec, Path(records_dir))
    cfg = work / "config.json"
    if config_path and cfg.is_file():
        dest = Path(config_path)
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(f".{dest.name}.restore-{os.getpid()}")
        try:
            shutil.copy2(cfg, tmp)
            os.replace(tmp, dest)
        finally:
            tmp.unlink(missing_ok=True)


def _replace_dir(src: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    new = dest.with_name(f".{dest.name}.restore-new-{os.getpid()}")
    old = dest.with_name(f".{dest.name}.restore-old-{os.getpid()}")
    for p in (new, old):
        if p.exists():
            shutil.rmtree(p)
    try:
        shutil.copytree(src, new)
        moved = False
        if dest.exists():
            os.replace(dest, old)
            moved = True
        try:
            os.replace(new, dest)
        except BaseException:
            if moved:
                os.replace(old, dest)
            raise
    finally:
        shutil.rmtree(new, ignore_errors=True)
    shutil.rmtree(old, ignore_errors=True)


# ---------------------------------------------------------------- restore

def restore_latest(home, drive_service, key, *, dest_store, records_dir=None,
                   config_path=None, swap: Callable | None = None,
                   budget_s: float | None = None,
                   clock=time.monotonic) -> RestoreResult:
    """Restore the newest restore point that verifies (see module docstring).

    Raises NoRestorePoint when none does, RestoreIncomplete when `budget_s`
    runs out mid-download, and lets transient Drive/network errors and the
    swap's own errors (RestoreBusyError) propagate: none of those is a reason
    to restore an older point.
    """
    swap = swap or backup.swap_in_store
    dest = Path(dest_store)
    dest.parent.mkdir(parents=True, exist_ok=True)
    stage = Path(str(dest) + ".stage")
    _remove_stage(stage)
    rdir = restore_dir(home)
    rdir.mkdir(parents=True, exist_ok=True)
    for p in rdir.glob("work-*"):     # plaintext left by a crash
        shutil.rmtree(p, ignore_errors=True)
    deadline = None if budget_s is None else clock() + budget_s
    want_records = bool(records_dir or config_path)
    fallbacks: list[tuple[Candidate, str]] = []
    bad: list[tuple[Candidate, str]] = []

    def skip(cand: Candidate, why: str) -> None:
        log.warning("backup restore: skipping %s %s (%s): %s", cand.kind,
                    cand.label, cand.file_id, why)
        fallbacks.append((cand, why))

    try:
        for cand, manifest in _ordered(home, drive_service, key, deadline=deadline,
                                       clock=clock, bad=bad):
            while bad:
                skip(*bad.pop(0))
            work = rdir / f"work-{cand.file_id}"
            shutil.rmtree(work, ignore_errors=True)
            work.mkdir()
            built = False
            try:
                if manifest is not None:
                    _build_from_manifest(drive_service, key, home, manifest, stage,
                                         work, want_records, deadline=deadline,
                                         clock=clock)
                else:
                    _build_from_legacy(drive_service, key, home, cand, stage, work,
                                       want_records, deadline=deadline, clock=clock)
                built = True
            except (RestoreIncomplete, _Transient):
                raise
            except OSError as exc:
                if _is_local_env_error(exc):
                    raise          # this machine's problem, not the candidate's
                _remove_stage(stage)
                skip(cand, f"{type(exc).__name__}: {exc}")
                continue
            except _Bad as exc:
                _remove_stage(stage)
                skip(cand, str(exc))
                continue
            except Exception as exc:  # noqa: BLE001 — any read/verify failure falls back
                _remove_stage(stage)
                skip(cand, f"{type(exc).__name__}: {exc}")
                continue
            finally:
                if not built:
                    _remove_stage(stage)
                    shutil.rmtree(work, ignore_errors=True)
            try:
                swap(stage, dest)
                _place_records(work, records_dir, config_path)
            finally:
                shutil.rmtree(work, ignore_errors=True)
            log.info("backup restore: restored %s %s (captured %s)%s", cand.kind,
                     cand.label, cand.captured_at,
                     f" after {len(fallbacks)} fallback(s)" if fallbacks else "")
            shutil.rmtree(rdir, ignore_errors=True)
            return RestoreResult(used=cand, fallbacks=fallbacks, path=dest)
        while bad:
            skip(*bad.pop(0))
    except _Transient as exc:
        raise exc.__cause__ from None
    finally:
        _remove_stage(stage)
    shutil.rmtree(rdir, ignore_errors=True)
    raise NoRestorePoint(
        "no restore point could be restored" + (
            ": " + "; ".join(f"{c.label}: {w}" for c, w in fallbacks) if fallbacks
            else " (none found)"), fallbacks)
