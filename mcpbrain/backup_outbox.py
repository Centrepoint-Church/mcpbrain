"""Local page-delta capture into the backup outbox (incremental backup).

Capture and upload are two independent jobs joined by a local OUTBOX. Capture
(this module) runs every `capture_interval_s`: it copies the live store with
SQLite's online backup API, diffs the copy page by page against the digests it
recorded last time (`pages.idx`), and writes the changed pages as encrypted
segment files plus one JSON sidecar per item. The uploader (a later task) ships
items oldest-first and deletes each local file once Drive has it. Nothing here
touches the network.

Layout under `<home>/backup/`:

    outbox/<chain>-<seq:06d>.json    item sidecar; its rename is the item's commit
    outbox/<item>-<tok>-<drive name> segment / records files the sidecar names
    work/                            scratch: the store copy, staging dirs
    pages.idx                        per-page digests of the last captured state
    capture.json                     CaptureState: where the chain is up to

**Local file names are not Drive names.** A segment dict carries `name` (the
name it gets on Drive, inside its chain folder: `base-0001`,
`delta-000005-0001`, `records-000005`) AND `file` (its name in the outbox).
They differ because Drive names are only unique within one chain folder, while
the outbox holds every chain at once: a store swapped in mid-upload starts a
second chain whose `base-0001` would overwrite the first chain's still-pending
`base-0001`, and coalescing writes `delta-<newest seq>` segments that must sit
beside the newest item's own `delta-<newest seq>` segments until the merged
sidecar commits. A per-write random token in `file` makes every local file
unique, so no write ever overwrites a file a committed sidecar names.

**Crash safety.** Order within a capture: segments (each temp + rename) ->
sidecar (temp + rename = commit) -> pages.idx -> capture.json. A crash before
the sidecar rename leaves only unreferenced files, swept by the next run. A
crash after it leaves a committed item capture.json does not know about, and
pages.idx either old or new. Diffing against the wrong index silently drops
pages (a delta of chain A taken against chain B's fresh base index omits every
page that changed between A's last item and B's base), so the index is
IDENTIFIED, never assumed: CaptureState and every sidecar record
`index_digest`, the sha256 of the index that capture produced. Before diffing:

- index digest == the state's: the index is the state's. Continue the state's
  chain after its newest committed item, and add every page in the segment
  ranges of items committed after the state to the changed set — a page that
  changed in such an item and changed BACK would otherwise be missed.
- index digest == a same-chain item committed after the state: the index is
  that item's. Continue after it (plus the ranges of any items after it).
- anything else (another chain's base, a damaged or foreign index): new base.

An item whose sidecar the uploader already deleted is fine as long as the
index still matches the state.
"""
from __future__ import annotations

import errno
import hashlib
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
from contextlib import nullcontext
from dataclasses import asdict, dataclass, field, fields
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

from mcpbrain import backup_pages

log = logging.getLogger(__name__)

_SPACE_MARGIN = 1.1
_COPY_NAME = "capture.sqlite3"


class OutboxFull(Exception):
    """The outbox holds `outbox_max_bytes` of un-uploaded items: capture pauses.

    Capture never deletes an un-uploaded item to make room — a laptop offline
    for a week keeps every restore point it captured and simply stops adding
    more until the uploader drains some.
    """


class LockBusy(Exception):
    """Raised by the caller's bounded lock when it cannot acquire in time.

    capture() lets it propagate before writing anything; the daemon treats it
    as "skip, retry next cycle", never as a failure.
    """


@dataclass
class Settings:
    """The `backup` config block, with the plan's defaults."""
    capture_interval_s: float = 3600
    retain_days: float = 7
    base_threshold: float = 2.0
    upload_budget_s: float = 60
    outbox_max_bytes: int = 2_000_000_000

    @classmethod
    def from_config(cls, cfg_backup: dict | None) -> "Settings":
        """`interval_s` is deliberately ignored: it was the full-upload cadence
        and no longer drives capture. `retain` (the old snapshot COUNT) is read
        as days when `retain_days` is absent, so an existing install keeps a
        comparable horizon without a config edit."""
        c = cfg_backup or {}
        d = cls()
        retain = c.get("retain_days", c.get("retain", d.retain_days))
        return cls(
            capture_interval_s=float(c.get("capture_interval_s", d.capture_interval_s)),
            retain_days=float(retain),
            base_threshold=float(c.get("base_threshold", d.base_threshold)),
            upload_budget_s=float(c.get("upload_budget_s", d.upload_budget_s)),
            outbox_max_bytes=int(c.get("outbox_max_bytes", d.outbox_max_bytes)),
        )


@dataclass
class Item:
    """One restore point's local payload: a base or a delta of one chain.

    `segments` entries: {name, file, size, sha256, md5, first_pgno, last_pgno,
    pages}; `records`: {name, file, size, sha256, md5} or None. `name` is the
    Drive name, `file` the outbox file name (see module docstring). `bytes` is
    the item's total ciphertext size.
    """
    name: str
    kind: Literal["base", "delta"]
    chain: str
    seq: int
    prev_seq: int | None
    captured_at: float
    page_size: int
    page_count: int
    db_sha256: str
    segments: list[dict] = field(default_factory=list)
    records: dict | None = None
    bytes: int = 0
    # sha256 of the pages.idx this capture produced (see module docstring).
    index_digest: str = ""

    def to_json(self) -> str:
        return json.dumps(asdict(self), sort_keys=True)

    @classmethod
    def from_json(cls, text: str) -> "Item":
        # Unknown keys are dropped, not fatal: an unreadable sidecar is skipped
        # by the sweep, which would then delete the files it names.
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in json.loads(text).items() if k in known})

    def files(self) -> list[str]:
        out = [s["file"] for s in self.segments]
        if self.records:
            out.append(self.records["file"])
        return out


@dataclass
class CaptureState:
    """Where the current chain is up to, persisted at `backup/capture.json`.

    `store_id` is the store file's `st_dev:st_ino`. `swap_in_store` and
    `bin/optimise_store.py --swap` both install a store with os.replace, which
    changes the inode, so a restored or rebuilt store always starts a new chain
    instead of a delta against pages the chain never held.
    """
    chain: str
    seq: int
    page_size: int
    page_count: int
    db_sha256: str
    records_fp: str
    base_bytes: int
    delta_bytes: int
    store_id: str
    # sha256 of pages.idx as this state left it; "" never matches = new base.
    index_digest: str = ""


def backup_dir(home) -> Path:
    return Path(home) / "backup"


def outbox_dir(home) -> Path:
    return backup_dir(home) / "outbox"


def work_dir(home) -> Path:
    return backup_dir(home) / "work"


def _write_atomic(path: Path, data: bytes) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)


def _load_state(home) -> CaptureState | None:
    try:
        return CaptureState(**json.loads(
            (backup_dir(home) / "capture.json").read_text()))
    except (FileNotFoundError, ValueError, TypeError):
        # Unreadable state = no chain: the next capture is a base. Never a
        # delta against a state we cannot describe.
        return None


def _save_state(home, state: CaptureState) -> None:
    _write_atomic(backup_dir(home) / "capture.json",
                  json.dumps(asdict(state), sort_keys=True).encode())


def _write_sidecar(home, item: Item) -> None:
    _write_atomic(outbox_dir(home) / f"{item.name}.json", item.to_json().encode())


def _superseded(item: Item, others: list[Item]) -> bool:
    """A coalesced item covers (prev_seq, seq]; anything of its chain strictly
    inside that range is a merged-away item whose sidecar outlived a crash."""
    for o in others:
        if (o is not item and o.chain == item.chain and o.prev_seq is not None
                and o.prev_seq < item.seq < o.seq):
            return True
    return False


def _read_sidecars(home) -> list[tuple[Path, Item]]:
    out = []
    ob = outbox_dir(home)
    if not ob.is_dir():
        return out
    for p in sorted(ob.glob("*.json")):
        try:
            out.append((p, Item.from_json(p.read_text())))
        except (ValueError, TypeError, KeyError):
            continue
    return out


def _order(items: list[Item]) -> list[Item]:
    first: dict[str, float] = {}
    for it in items:
        first[it.chain] = min(first.get(it.chain, it.captured_at), it.captured_at)
    return sorted(items, key=lambda i: (first[i.chain], i.chain, i.seq))


def pending_items(home) -> list[Item]:
    """Every committed, not-yet-uploaded item, oldest chain first, then seq."""
    items = [it for _p, it in _read_sidecars(home)]
    return _order([it for it in items if not _superseded(it, items)])


def outbox_bytes(home) -> int:
    ob = outbox_dir(home)
    if not ob.is_dir():
        return 0
    return sum(p.stat().st_size for p in ob.iterdir() if p.is_file())


def _quarantine_bad_sidecars(ob: Path) -> None:
    for p in sorted(ob.glob("*.json")):
        try:
            Item.from_json(p.read_text())
        except (ValueError, TypeError, KeyError) as e:
            bad = p.with_name(p.name + ".bad")
            os.replace(p, bad)
            log.warning("backup outbox: unreadable sidecar %s quarantined as %s "
                        "(%s); unnamed outbox files are kept until it is resolved",
                        p.name, bad.name, e)


def _sweep(home) -> None:
    """Drop what a crash left behind: temp files, files no live sidecar names,
    sidecars a committed coalesce superseded, and anything in work/.

    Only ever removes data no committed item depends on, so it is safe to run
    before the lock is taken (a LockBusy afterwards loses nothing).
    """
    ob = outbox_dir(home)
    if ob.is_dir():
        _quarantine_bad_sidecars(ob)
        # A quarantined sidecar may name files we cannot see: delete nothing
        # unnamed while one exists, or a committed item's segments go with it.
        protect = any(ob.glob("*.json.bad"))
        pairs = _read_sidecars(home)
        items = [it for _p, it in pairs]
        live: set[str] = set()
        for p, it in pairs:
            if _superseded(it, items):
                p.unlink(missing_ok=True)
            else:
                live.update(it.files())
        for p in ob.iterdir():
            if not p.is_file():
                continue
            if p.name.endswith(".tmp"):
                p.unlink(missing_ok=True)
            elif p.suffix == ".json":
                continue
            elif p.name not in live and not protect:
                p.unlink(missing_ok=True)
    wd = work_dir(home)
    if wd.is_dir():
        for p in wd.iterdir():
            if p.is_dir() and not p.is_symlink():
                shutil.rmtree(p, ignore_errors=True)
            else:
                p.unlink(missing_ok=True)


def _copy_store(store_path, dst: Path) -> None:
    """One consistent read transaction via the online backup API.

    Store-free on purpose: a read-only URI connection needs no schema init and
    no sqlite-vec extension to copy pages, and it reads through the WAL.
    """
    src = sqlite3.connect(f"{Path(store_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        out = sqlite3.connect(dst)
        try:
            src.backup(out)
        finally:
            out.close()
    finally:
        src.close()


def _logical_size(store_path) -> int:
    """page_count x page_size: what the copy will occupy. The main file alone
    can be far smaller in WAL mode, where committed pages live in the -wal."""
    con = sqlite3.connect(f"{Path(store_path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        n = con.execute("PRAGMA page_count").fetchone()[0]
        ps = con.execute("PRAGMA page_size").fetchone()[0]
    finally:
        con.close()
    return int(n) * int(ps)


def _remove_copy(copy: Path) -> None:
    for suffix in ("", "-journal", "-wal", "-shm"):
        Path(str(copy) + suffix).unlink(missing_ok=True)


def _token() -> str:
    return secrets.token_hex(4)


def _seg_dict(s, local: str) -> dict:
    return {"name": s.name, "file": local, "size": s.size, "sha256": s.sha256,
            "md5": s.md5, "first_pgno": s.first_pgno, "last_pgno": s.last_pgno,
            "pages": s.pages}


def _write_pages(home, src, pgnos, *, item_name: str, prefix: str, key: bytes,
                 header: dict) -> list[dict]:
    """write_segments into a staging dir, then move each into the outbox under
    a unique local name. Staging keeps write_segments' Drive-shaped names
    (`<prefix>-NNNN`) without letting them collide in the shared outbox."""
    pgnos = sorted(pgnos)
    if not pgnos:
        return []
    tok = _token()
    stage = work_dir(home) / f"stage-{tok}"
    try:
        segs = backup_pages.write_segments(src, pgnos, out_dir=stage, prefix=prefix,
                                           key=key, header=header)
        out = []
        for s in segs:
            local = f"{item_name}-{tok}-{s.name}"
            os.replace(s.path, outbox_dir(home) / local)
            out.append(_seg_dict(s, local))
        return out
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def _file_hashes(path: Path) -> tuple[str, str]:
    sha, md5 = hashlib.sha256(), hashlib.md5()
    with path.open("rb") as f:
        for blk in iter(lambda: f.read(1 << 20), b""):
            sha.update(blk)
            md5.update(blk)
    return sha.hexdigest(), md5.hexdigest()


def _write_records(home, records_dir, config_path, *, item_name: str, seq: int,
                   key: bytes) -> dict:
    name = f"records-{seq:06d}"
    local = f"{item_name}-{_token()}-{name}"
    final = outbox_dir(home) / local
    tmp = outbox_dir(home) / (local + ".tmp")
    sha, size = backup_pages.records_tar(records_dir, config_path, tmp, key)
    os.replace(tmp, final)
    _sha, md5 = _file_hashes(final)
    return {"name": name, "file": local, "size": size, "sha256": sha, "md5": md5}


def _discard(home, files) -> None:
    for f in files:
        (outbox_dir(home) / f).unlink(missing_ok=True)


def _new_chain(now: float) -> str:
    day = datetime.fromtimestamp(now, tz=timezone.utc).strftime("%Y%m%d")
    return f"chain-{day}-{secrets.token_hex(3)}"


def capture(home, store_path, key: bytes, *, lock=nullcontext(),
            settings: Settings | None = None, clock=time.time,
            records_dir=None, config_path=None) -> Item | None:
    """Capture the store's changed pages (and records, if changed) as one item.

    Returns the committed Item, or None when nothing changed. Raises
    OutboxFull when the outbox is at its cap, OSError(ENOSPC) before copying
    when the work dir cannot hold the copy, and lets the lock's LockBusy
    propagate before anything is written. `lock` is held for the copy only:
    hashing, compression and encryption run on the private copy, outside it.
    """
    settings = settings or Settings()
    if records_dir is None:
        from mcpbrain import config
        records_dir = config.records_dir(home)
    if config_path is None:
        config_path = Path(home) / "config.json"

    _sweep(home)
    if outbox_bytes(home) >= settings.outbox_max_bytes:
        raise OutboxFull(
            f"backup outbox holds {outbox_bytes(home)} bytes "
            f"(cap {settings.outbox_max_bytes}); capture paused until upload drains it")
    wd = work_dir(home)
    wd.mkdir(parents=True, exist_ok=True)
    outbox_dir(home).mkdir(parents=True, exist_ok=True)
    need = int(_logical_size(store_path) * _SPACE_MARGIN)
    free = shutil.disk_usage(str(wd)).free
    if free < need:
        raise OSError(errno.ENOSPC,
                      f"backup capture needs {need} bytes free in {wd}, has {free}")

    copy = wd / _COPY_NAME
    try:
        with lock:
            st = os.stat(store_path)
            _copy_store(store_path, copy)
        store_id = f"{st.st_dev}:{st.st_ino}"
        return _capture_copy(home, copy, key, settings=settings, now=clock(),
                             store_id=store_id, records_dir=records_dir,
                             config_path=config_path)
    finally:
        _remove_copy(copy)


def _capture_copy(home, copy: Path, key: bytes, *, settings: Settings, now: float,
                  store_id: str, records_dir, config_path) -> Item | None:
    bdir = backup_dir(home)
    idx_path = bdir / "pages.idx"
    state = _load_state(home)
    fp = backup_pages.records_fingerprint(records_dir, config_path)

    # Items of the state's chain committed after the last saved state (a crash
    # between the sidecar rename and capture.json). See the module docstring.
    since: list[Item] = []   # every such item: they are in the chain now
    later: list[Item] = []   # those the index does not reflect: union their pages
    base = (state is None or not idx_path.exists() or state.store_id != store_id)
    index = backup_pages.PageIndex()
    if not base:
        index = backup_pages.PageIndex.load(idx_path)
        h = index.digest()
        since = sorted((it for it in pending_items(home)
                        if it.chain == state.chain and it.seq > state.seq),
                       key=lambda i: i.seq)
        if h == state.index_digest:
            anchor = state.seq
        else:
            match = [it.seq for it in since if it.index_digest == h]
            if match:
                anchor = match[-1]
            else:
                base = True
                index = backup_pages.PageIndex()
        if not base:
            later = [it for it in since if it.seq > anchor]
    scan = backup_pages.scan_changes(copy, index)

    if not base:
        prev_count = since[-1].page_count if since else state.page_count
        # A shrink means pages the chain holds no longer exist; a page_size
        # change makes every page a different unit. Neither is a delta.
        if scan.page_size != state.page_size or scan.page_count < prev_count:
            base = True

    if not base:
        changed = set(scan.changed)
        for it in later:
            for s in it.segments:
                changed.update(range(s["first_pgno"],
                                     min(s["last_pgno"], scan.page_count) + 1))
        if not changed and scan.page_count == prev_count and fp == state.records_fp:
            return None
        prev_seq = since[-1].seq if since else state.seq
        seq = prev_seq + 1
        so_far = state.delta_bytes + sum(
            s["size"] for it in since for s in it.segments)
        item_name = f"{state.chain}-{seq:06d}"
        header = {"chain": state.chain, "seq": seq, "kind": "delta",
                  "page_count": scan.page_count}
        segs = _write_pages(home, copy, changed, item_name=item_name,
                            prefix=f"delta-{seq:06d}", key=key, header=header)
        seg_bytes = sum(s["size"] for s in segs)
        if so_far + seg_bytes > settings.base_threshold * state.base_bytes:
            # Exact, not estimated: compare compressed bytes with compressed
            # bytes. Writing the delta first costs one small throwaway write,
            # once per chain.
            _discard(home, [s["file"] for s in segs])
            base = True
        else:
            records = None
            if fp != state.records_fp:
                records = _write_records(home, records_dir, config_path,
                                         item_name=item_name, seq=seq, key=key)
            item = Item(name=item_name, kind="delta", chain=state.chain, seq=seq,
                        prev_seq=prev_seq, captured_at=now, page_size=scan.page_size,
                        page_count=scan.page_count, db_sha256=scan.db_sha256,
                        segments=segs, records=records,
                        bytes=seg_bytes + (records["size"] if records else 0))
            new_state = CaptureState(
                chain=state.chain, seq=seq, page_size=scan.page_size,
                page_count=scan.page_count, db_sha256=scan.db_sha256,
                records_fp=fp, base_bytes=state.base_bytes,
                delta_bytes=so_far + seg_bytes, store_id=store_id)
            _commit(home, item, scan.new_index, new_state)
            return item

    chain = _new_chain(now)
    item_name = f"{chain}-{1:06d}"
    header = {"chain": chain, "seq": 1, "kind": "base",
              "page_count": scan.page_count}
    segs = _write_pages(home, copy, range(1, scan.page_count + 1),
                        item_name=item_name, prefix="base", key=key, header=header)
    seg_bytes = sum(s["size"] for s in segs)
    # A base always carries records: a restore point is base + deltas + the
    # newest records item of ITS chain, and an older chain may be pruned.
    records = _write_records(home, records_dir, config_path, item_name=item_name,
                             seq=1, key=key)
    item = Item(name=item_name, kind="base", chain=chain, seq=1, prev_seq=None,
                captured_at=now, page_size=scan.page_size,
                page_count=scan.page_count, db_sha256=scan.db_sha256,
                segments=segs, records=records, bytes=seg_bytes + records["size"])
    new_state = CaptureState(
        chain=chain, seq=1, page_size=scan.page_size, page_count=scan.page_count,
        db_sha256=scan.db_sha256, records_fp=fp, base_bytes=seg_bytes,
        delta_bytes=0, store_id=store_id)
    _commit(home, item, scan.new_index, new_state)
    return item


def _commit(home, item: Item, index, state: CaptureState) -> None:
    """Sidecar (the commit), then pages.idx, then capture.json — in that order.

    Both the sidecar and the state carry the new index's digest, so the next
    capture can tell which of them (if either) pages.idx belongs to."""
    item.index_digest = state.index_digest = index.digest()
    _write_sidecar(home, item)
    index.save(backup_dir(home) / "pages.idx")
    _save_state(home, state)


def _local_complete(home, item: Item) -> bool:
    ob = outbox_dir(home)
    return all((ob / f).is_file() for f in item.files())


def _sqlite_header(page_size: int) -> bytes:
    """Enough of a SQLite header for write_segments to read the page size when
    page 1 itself is not among the merged pages (it is never written out)."""
    enc = 1 if page_size == 65536 else page_size
    return b"SQLite format 3\x00" + enc.to_bytes(2, "big") + b"\x00" * 82


def coalesce(home, key: bytes, *, started: set[str], deadline: float | None = None,
             clock=time.monotonic) -> Item | None:
    """Merge the first run of >=2 consecutive, not-started deltas of one chain.

    Newest page wins; the merged item takes the newest merged item's name,
    seq, page_count, db_sha256 and capture time, the oldest's prev_seq, and
    the newest non-None records. Order: merged segments (unique local names) ->
    merged sidecar renamed over the newest's sidecar (the commit) -> older
    sidecars deleted -> files nothing names any more swept. A crash after the
    commit leaves older sidecars that `_superseded` hides and the next sweep
    removes. Returns the merged Item, or None when there is nothing to merge.

    `deadline` (on `clock`) bounds the work: once it passes, the run is cut
    after the item just read (never before two, so every call that starts
    makes progress). The items left out stay pending, still following the
    merged item, and a later call can merge them.
    """
    _sweep(home)
    run = _first_run(home, started)
    if run is None:
        return None
    ps = run[-1].page_size
    work_dir(home).mkdir(parents=True, exist_ok=True)
    sparse = work_dir(home) / f"coalesce-{_token()}.pages"
    seen: set[int] = set()
    merged_run: list[Item] = []
    try:
        with sparse.open("wb") as f:
            for it in run:
                if (deadline is not None and len(merged_run) >= 2
                        and clock() >= deadline):
                    break
                if it.page_size != ps:
                    raise ValueError(f"{it.name}: page_size differs within a chain")
                for s in it.segments:
                    hdr, pages = backup_pages.read_segment(
                        outbox_dir(home) / s["file"], key)
                    with pages:
                        if int(hdr["page_size"]) != ps:
                            raise ValueError(f"{s['file']}: page_size mismatch")
                        for pgno, page in pages:
                            if pgno > it.page_count:
                                continue
                            f.seek((pgno - 1) * ps)
                            f.write(page)
                            seen.add(pgno)
                merged_run.append(it)
            if 1 not in seen:
                f.seek(0)
                f.write(_sqlite_header(ps))
        newest = merged_run[-1]
        count = newest.page_count
        header = {"chain": newest.chain, "seq": newest.seq, "kind": "delta",
                  "page_count": count}
        segs = _write_pages(home, sparse, {p for p in seen if p <= count},
                            item_name=newest.name,
                            prefix=f"delta-{newest.seq:06d}", key=key, header=header)
    finally:
        sparse.unlink(missing_ok=True)
    run = merged_run
    records = next((it.records for it in reversed(run) if it.records), None)
    merged = Item(name=newest.name, kind="delta", chain=newest.chain, seq=newest.seq,
                  prev_seq=run[0].prev_seq, captured_at=newest.captured_at,
                  page_size=ps, page_count=count, db_sha256=newest.db_sha256,
                  segments=segs, records=records,
                  bytes=sum(s["size"] for s in segs) + (records["size"] if records else 0),
                  # the merged item ends at the newest's state, so pages.idx
                  # (if it was saved for the newest) still identifies it
                  index_digest=newest.index_digest)
    _write_sidecar(home, merged)
    for it in run[:-1]:
        (outbox_dir(home) / f"{it.name}.json").unlink(missing_ok=True)
    _sweep(home)
    return merged


def _first_run(home, started: set[str]) -> list[Item] | None:
    items = pending_items(home)
    run: list[Item] = []
    for it in items:
        ok = (it.kind == "delta" and it.name not in started
              and _local_complete(home, it))
        if ok and run and run[-1].chain == it.chain and it.prev_seq == run[-1].seq:
            run.append(it)
            continue
        if len(run) >= 2:
            return run
        run = [it] if ok else []
    return run if len(run) >= 2 else None
