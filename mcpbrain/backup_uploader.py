"""Time-boxed outbox uploader (incremental backup): journal, manifests, commit.

Capture (`backup_outbox`) leaves committed items in `backup/outbox/`. Each call
to `upload_step` spends at most `budget_s` shipping them, oldest first, to

    <drive root>/<person>/<chain>/{base-NNNN, delta-<seq>-NNNN, records-<seq>,
                                   manifest-<chain>-<seq:06d>}

and is safe to stop at ANY point: a lid close, a reboot, a daemon restart.

**Journal** (`backup/journal.json`, temp + os.replace on every write; NOT in
outbox/, whose sweep deletes every file no sidecar names)::

    {"chains": {chain: {"folder_id", "committed_seq", "files": [{name, file_id,
                 size, sha256, kind, seq}], "base_bytes", "first_captured_at",
                 "last_captured_at", "committed_at", "manifests": [{seq, file_id}]}},
     "inflight": {item_name: {drive_name: Session json | {"file_id", ...}}},
     "pending_deletes": [file_id, ...],
     "stalls": {"<item>/<drive_name>": consecutive no-progress steps}}

An inflight entry is either the resumable Session (resume by status query) or,
once Drive's size and md5 equal the local file's, the recorded file id — and
only then is the local file deleted.

**Commit order per item** (each step durable before the next):
every file verified -> manifest uploaded and verified (the REMOTE commit) ->
journal `committed_seq` -> capture.json dropped if it lags (R6) -> sidecar and
any local leftovers deleted -> `backup_state.json` ok + restore_point. A crash
anywhere before the journal write redoes at most the manifest upload (a
duplicate, identical manifest under the same name; restore reads either). On
any failure nothing local is deleted.

**Chain continuity.** An item is uploaded only when it follows exactly what
the journal says the chain has committed (`prev_seq == committed_seq`; a base
needs an uncommitted chain). Otherwise its predecessor is gone (sidecar lost,
journal lost, files missing) and no manifest built from it could be
restored: the item and every later item of that chain are discarded locally,
capture.json is deleted so the next capture is a base, and the gap is logged.

**Addressing.** Every Drive file is referenced by the id recorded at upload.
The only listing used is the one that finds an existing chain folder before
creating it; nothing is deleted or re-uploaded because a listing omitted it.
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from typing import Callable

from googleapiclient.http import MediaInMemoryUpload

from mcpbrain import backup, backup_location, backup_outbox as bo, drive_resumable as dr
from mcpbrain.backup_state import write_backup_state

log = logging.getLogger(__name__)

_NUM_RETRIES = backup._NUM_RETRIES
_OCTET = "application/octet-stream"
# Coalesce merges one run per call; bound the loop so a pathological outbox
# cannot spin the step.
_MAX_COALESCE = 64


class UploadFailed(Exception):
    """A file or manifest Drive holds does not match what we sent."""


@dataclass
class StepResult:
    committed: list[str]
    progress: dict | None
    error: str | None
    pending: int
    pending_bytes: int


def journal_path(home):
    return bo.backup_dir(home) / "journal.json"


def _empty() -> dict:
    return {"chains": {}, "inflight": {}, "pending_deletes": [], "stalls": {}}


def load_journal(home) -> dict:
    """The journal, or an empty one. An unreadable journal is set aside as
    `journal.json.bad` (never silently overwritten); losing it costs re-upload
    and, via the continuity check, a new base — never a wrong restore point."""
    p = journal_path(home)
    try:
        j = json.loads(p.read_text())
        if not isinstance(j, dict):
            raise ValueError("journal is not an object")
    except FileNotFoundError:
        return _empty()
    except (OSError, ValueError) as exc:
        log.warning("backup journal %s unreadable (%s); set aside as %s.bad",
                    p, exc, p.name)
        try:
            os.replace(p, p.with_name(p.name + ".bad"))
        except OSError:
            pass
        return _empty()
    for k, v in _empty().items():
        if not isinstance(j.get(k), type(v)):
            j[k] = v
    return j


def save_journal(home, j: dict) -> None:
    bo.backup_dir(home).mkdir(parents=True, exist_ok=True)
    bo._write_atomic(journal_path(home), json.dumps(j, sort_keys=True).encode())


def _item_files(item: bo.Item) -> list[tuple[dict, str]]:
    """(spec, kind) in upload / apply order: segments, then records."""
    out = [(s, item.kind) for s in item.segments]
    if item.records:
        out.append((item.records, "records"))
    return out


def _confirm(svc, meta: dict, path) -> dict:
    """Verify an uploaded file against `path`. Drive's completion (notably a
    status query on an already-finished session) may omit size/md5Checksum:
    re-read them before deciding. Raises dr.UploadMismatch only on a real
    size/md5 disagreement, UploadFailed when Drive still will not say."""
    if not (meta or {}).get("size") or not (meta or {}).get("md5Checksum"):
        meta = svc.files().get(fileId=meta["id"], fields="id,size,md5Checksum",
                               supportsAllDrives=True).execute(num_retries=_NUM_RETRIES)
        if not (meta or {}).get("size") or not (meta or {}).get("md5Checksum"):
            raise UploadFailed(f"Drive returned no size/md5Checksum for {path}")
    dr.verify(meta, path)
    return meta


def _delete_remote(svc, j: dict, file_id: str) -> None:
    """Best effort; a failure leaves the id on `pending_deletes` for prune."""
    try:
        svc.files().delete(fileId=file_id, supportsAllDrives=True).execute(
            num_retries=_NUM_RETRIES)
    except Exception as exc:  # noqa: BLE001 — best effort by design
        log.warning("backup: could not delete Drive file %s (%s); queued", file_id, exc)
        if file_id not in j["pending_deletes"]:
            j["pending_deletes"].append(file_id)


def _continuous(j: dict, item: bo.Item) -> bool:
    committed = int((j["chains"].get(item.chain) or {}).get("committed_seq") or 0)
    if item.kind == "base":
        return committed == 0 and item.prev_seq is None
    return committed > 0 and item.prev_seq == committed


def _locally_complete(home, j: dict, item: bo.Item) -> bool:
    done = j["inflight"].get(item.name) or {}
    ob = bo.outbox_dir(home)
    return all("file_id" in (done.get(spec["name"]) or {}) or (ob / spec["file"]).is_file()
               for spec, _k in _item_files(item))


def _drop_capture_state(home) -> None:
    (bo.backup_dir(home) / "capture.json").unlink(missing_ok=True)


def _delete_local(home, item: bo.Item) -> None:
    ob = bo.outbox_dir(home)
    for f in item.files():
        (ob / f).unlink(missing_ok=True)
    (ob / f"{item.name}.json").unlink(missing_ok=True)


def _discard_from(home, j: dict, item: bo.Item, why: str) -> int:
    """Discard `item` and every later item of its chain; next capture = base."""
    victims = [it for it in bo.pending_items(home)
               if it.chain == item.chain and it.seq >= item.seq]
    for it in victims:
        for entry in (j["inflight"].pop(it.name, None) or {}).values():
            fid = entry.get("file_id") if isinstance(entry, dict) else None
            if fid and fid not in j["pending_deletes"]:
                j["pending_deletes"].append(fid)
        for k in [k for k in j["stalls"] if k.startswith(f"{it.name}/")]:
            del j["stalls"][k]
    save_journal(home, j)
    # capture.json first: a crash after it but before the sidecars go still
    # leaves the next capture a base.
    _drop_capture_state(home)
    for it in victims:
        _delete_local(home, it)
    log.warning("backup chain %s has a gap at %s (%s); discarded %d item(s), "
                "the next capture starts a new base", item.chain, item.name, why,
                len(victims))
    return len(victims)


def _chain_folder(svc, home, j: dict, loc, chain: str) -> str:
    entry = j["chains"].get(chain)
    if entry and entry.get("folder_id"):
        return entry["folder_id"]
    fid = backup_location._find_folder(svc, chain, loc.folder_id, drive_id=loc.drive_id)
    if fid is None:
        fid = svc.files().create(
            body={"name": chain, "mimeType": backup.FOLDER_MIME,
                  "parents": [loc.folder_id]},
            supportsAllDrives=True, fields="id",
        ).execute(num_retries=_NUM_RETRIES)["id"]
    j["chains"][chain] = {
        "folder_id": fid, "committed_seq": 0, "files": [], "base_bytes": 0,
        "first_captured_at": None, "last_captured_at": None, "committed_at": None,
        "manifests": [],
    }
    save_journal(home, j)
    return fid


def _manifest_files(chain_entry: dict, new: list[dict]) -> list[dict]:
    """Base segments, every delta up to this item, then the newest records."""
    every = chain_entry["files"] + new
    pages = [f for f in every if f["kind"] != "records"]
    records = [f for f in every if f["kind"] == "records"]
    return pages + records[-1:]


def _upload_manifest(home, svc, key, j: dict, item: bo.Item, folder: str,
                     files: list[dict]) -> str:
    manifest = {
        "format": 1, "chain": item.chain, "seq": item.seq,
        "captured_at": item.captured_at, "page_size": item.page_size,
        "page_count": item.page_count, "db_sha256": item.db_sha256,
        "files": files,
    }
    name = f"manifest-{item.chain}-{item.seq:06d}"
    # backup_dir, not work/: capture's sweep empties work/.
    tmp = bo.backup_dir(home) / "manifest.tmp.enc"
    try:
        with backup.open_encrypted(tmp, key) as w:
            w.write(json.dumps(manifest, sort_keys=True).encode())
        data = tmp.read_bytes()
        meta = svc.files().create(
            body={"name": name, "parents": [folder]},
            media_body=MediaInMemoryUpload(data, mimetype=_OCTET, resumable=False),
            supportsAllDrives=True, fields="id,size,md5Checksum",
        ).execute(num_retries=_NUM_RETRIES)
        try:
            meta = _confirm(svc, meta, tmp)
        except Exception:
            # Not recorded anywhere yet: a manifest we cannot vouch for must
            # not sit on Drive looking like a commit.
            if (meta or {}).get("id"):
                _delete_remote(svc, j, meta["id"])
            raise
        return meta["id"]
    finally:
        tmp.unlink(missing_ok=True)


def upload_step(home, *, drive_service, transport, key, budget_s,
                clock=time.monotonic, wall=time.time,
                on_progress: Callable[[dict], None] | None = None,
                sleep=time.sleep) -> StepResult:
    """Upload outbox items oldest first for at most `budget_s` (on `clock`).

    Commits each item whose files all verify, by uploading its manifest. Any
    failure stops the step, records `backup_state` ok=False once, and deletes
    nothing local. A step that confirmed no byte at all while an upload hit
    its deadline or failed is recorded as a failure too (R9): `upload_file`
    returns rather than sleeping past the deadline, so a dead link otherwise
    never raises.
    """
    deadline = clock() + budget_s
    committed: list[str] = []
    state = {"progress": None, "moved": False, "stalled": None}

    if not bo.pending_items(home):
        return _result(home, committed, None, None)

    j = load_journal(home)
    error: str | None = None
    try:
        loc = backup_location.resolve(home, drive_service, create=True)
        for _ in range(_MAX_COALESCE):
            if bo.coalesce(home, key, started=set(j["inflight"])) is None:
                break
        while clock() < deadline:
            items = bo.pending_items(home)
            if not items:
                break
            item = items[0]
            if not _continuous(j, item):
                have = (j["chains"].get(item.chain) or {}).get("committed_seq") or 0
                _discard_from(home, j, item,
                              f"follows seq {item.prev_seq}, chain has {have} committed")
                continue
            if not _locally_complete(home, j, item):
                _discard_from(home, j, item, "local files missing")
                continue
            if not _upload_item(home, drive_service, transport, key, j, loc, item,
                                deadline=deadline, clock=clock, sleep=sleep,
                                wall=wall, on_progress=on_progress, state=state):
                break
            committed.append(item.name)
    except Exception as exc:  # noqa: BLE001 — recorded, surfaced, retried next step
        error = f"{type(exc).__name__}: {exc}"
        log.warning("backup upload failed: %s", error)

    if error is None and state["stalled"] and not state["moved"]:
        error = state["stalled"]
    elif error is not None and not state["moved"]:
        error = f"upload made no progress: {error}"
    if error is not None:
        try:
            save_journal(home, j)
        except OSError as exc:
            log.warning("backup journal write failed: %s", exc)
        write_backup_state(home, ok=False, error=error)
    return _result(home, committed, state["progress"], error)


def _upload_item(home, svc, transport, key, j, loc, item: bo.Item, *, deadline,
                 clock, sleep, wall, on_progress, state) -> bool:
    """Upload, verify and commit `item`. False = budget ran out first."""
    folder = _chain_folder(svc, home, j, loc, item.chain)
    done = j["inflight"].setdefault(item.name, {})
    save_journal(home, j)
    ob = bo.outbox_dir(home)

    for spec, _kind in _item_files(item):
        name = spec["name"]
        if "file_id" in (done.get(name) or {}):
            continue
        path = ob / spec["file"]
        stall_key = f"{item.name}/{name}"
        prior = done.get(name)
        session = dr.Session.from_json(prior) if prior else None
        best = {"offset": session.offset if session else 0, "moved": False}

        def report(sent, total, _item=item.name, _name=name):
            state["progress"] = {"item": _item, "file": _name, "sent": sent,
                                 "total": total}
            if on_progress is not None:
                on_progress(dict(state["progress"]))

        def save(s, _name=name, _best=best, _size=spec["size"], _report=report):
            done[_name] = s.to_json()
            if s.offset > _best["offset"]:
                _best["offset"], _best["moved"] = s.offset, True
                state["moved"] = True
            save_journal(home, j)
            _report(s.offset, _size)

        try:
            meta = dr.upload_file(transport, path, name=name, parent_id=folder,
                                  session=session, save=save, deadline=deadline,
                                  clock=clock, sleep=sleep)
        except Exception:
            _note_stall(j, stall_key, best["moved"])
            raise
        if meta is None:
            n = _note_stall(j, stall_key, best["moved"])
            save_journal(home, j)
            if n:
                state["stalled"] = (
                    f"upload made no progress on {name} ({n} step(s) in a row): no "
                    "bytes confirmed before the upload budget ran out (Drive "
                    "unreachable or failing)")
            return False
        try:
            meta = _confirm(svc, meta, path)
        except dr.UploadMismatch:
            # Drive holds different bytes: that file id is worthless. Drop it
            # and the session so the next step uploads the file afresh.
            done.pop(name, None)
            _delete_remote(svc, j, meta["id"])
            save_journal(home, j)
            raise
        done[name] = {"file_id": meta["id"], "size": spec["size"],
                      "sha256": spec["sha256"]}
        j["stalls"].pop(stall_key, None)
        state["moved"] = True
        save_journal(home, j)
        path.unlink(missing_ok=True)
        report(spec["size"], spec["size"])

    # Every file verified: commit even if the budget just ran out (the
    # manifest is a few KB; a later file would have stopped in upload_file).
    _commit(home, svc, key, j, item, folder, wall)
    state["moved"] = True
    return True


def _note_stall(j: dict, key: str, moved: bool) -> int:
    if moved:
        j["stalls"].pop(key, None)
        return 0
    j["stalls"][key] = int(j["stalls"].get(key) or 0) + 1
    return j["stalls"][key]


def _commit(home, svc, key, j: dict, item: bo.Item, folder: str, wall) -> None:
    done = j["inflight"][item.name]
    new = [{"name": spec["name"], "file_id": done[spec["name"]]["file_id"],
            "size": spec["size"], "sha256": spec["sha256"], "kind": kind,
            "seq": item.seq}
           for spec, kind in _item_files(item)]
    entry = j["chains"][item.chain]
    manifest_id = _upload_manifest(home, svc, key, j, item, folder,
                                   _manifest_files(entry, new))

    # Remote commit landed; record it.
    entry["files"].extend(new)
    entry["committed_seq"] = item.seq
    if item.kind == "base":
        entry["base_bytes"] = sum(s["size"] for s in item.segments)
    entry["first_captured_at"] = entry.get("first_captured_at") or item.captured_at
    entry["last_captured_at"] = item.captured_at
    entry["committed_at"] = wall()
    entry.setdefault("manifests", []).append({"seq": item.seq, "file_id": manifest_id})
    j["inflight"].pop(item.name, None)
    for k in [k for k in j["stalls"] if k.startswith(f"{item.name}/")]:
        del j["stalls"][k]
    save_journal(home, j)

    # R6: capture.json behind this item (a crash between its sidecar rename and
    # capture.json) would, once the sidecar is gone, let the next capture reuse
    # this seq. Drop it before the sidecar so the next capture is a base.
    cs = bo._load_state(home)
    if cs is not None and cs.chain == item.chain and item.seq > cs.seq:
        _drop_capture_state(home)
    _delete_local(home, item)
    write_backup_state(home, ok=True, restore_point=item.captured_at)


def _result(home, committed, progress, error) -> StepResult:
    items = bo.pending_items(home)
    ob = bo.outbox_dir(home)
    pending_bytes = 0
    for it in items:
        for f in it.files():
            try:
                pending_bytes += (ob / f).stat().st_size
            except OSError:
                pass
    return StepResult(committed=committed, progress=progress, error=error,
                      pending=len(items), pending_bytes=pending_bytes)
