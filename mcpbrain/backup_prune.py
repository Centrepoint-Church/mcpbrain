"""Stepwise prune of the incremental backup layout (whole chains + legacy).

Runs after a commit, spends at most `budget_s`, and is safe to stop anywhere:
everything it means to delete is first written to the journal's
`pending_deletes` list, and each id leaves that list only once Drive has
deleted it (or answered 404, which is the same outcome). The next call carries
on from the list.

**What is deleted, and why it is safe.** When in doubt, keep.

* Journal chains (ordered by `first_captured_at`) that have committed
  (`committed_seq >= 1`): the newest is always kept; any chain whose newest
  restore point (`last_captured_at`) is inside `retain_days` is kept; an
  older one is deleted only when a NEWER chain has committed — true for every
  chain but the newest committed one, so a chain is never removed before its
  successor is restorable.
* Journal chains that never committed hold no restore point. One is deleted
  only when the day its id names ended before the retention cutoff AND a
  committed chain began after that day (the uploader works oldest first, so
  a chain overtaken by a committed successor can never progress). An id that
  does not parse is kept.
* `chain-*` folders under the person folder that the journal does not know
  (lost journal, an earlier install): deleted only when the journal's newest
  committed chain began after the folder's newest activity and that activity
  (newest child createdTime, or the folder's own times) is older than
  `retain_days`. Absence from a listing is never a reason to delete — only
  presence plus age.
* Legacy single-file `.enc` backups (in `Location.legacy_folders` and loose in
  the person folder) once the new layout's oldest committed restore point is
  older than `retain_days`, i.e. the new layout already covers retention.
* Ids the uploader queued (orphan uploads it could not delete).

Prune never deletes individual manifests or chain files — only whole chain
folders (R12). A duplicate manifest from a crash goes with its folder.

**Hold.** If the newest committed chain's base is under `_HOLD_RATIO` of the
previous committed chain's base, something may have shrunk the store; nothing
at all is deleted and the result carries `held`.

**Journal.** A chain's entry is removed when its folder's deletion completes,
and each live chain's `files` keeps only its newest records entry (the only
one a new manifest names); both keep the journal from growing forever.
"""
from __future__ import annotations

import logging
import re
import time
from datetime import datetime, timezone
from typing import Callable

from mcpbrain import backup, backup_location
from mcpbrain import backup_uploader as bu

log = logging.getLogger(__name__)

_NUM_RETRIES = backup._NUM_RETRIES
_HOLD_RATIO = 0.5
_DAY = 86400.0
_CHAIN_ID = re.compile(r"^chain-(\d{8})-[0-9a-f]+$")
_LIST_FIELDS = "nextPageToken, files(id, name, mimeType, createdTime, modifiedTime)"


def _is_not_found(exc: Exception) -> bool:
    status = getattr(getattr(exc, "resp", None), "status", None)
    try:
        return int(status) == 404
    except (TypeError, ValueError):
        return False


def _ts(value) -> float | None:
    """Epoch seconds of a Drive RFC 3339 time, or None."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _id_day_end(chain: str) -> float | None:
    """End (UTC) of the day a chain id names, or None when it does not parse."""
    m = _CHAIN_ID.match(chain)
    if not m:
        return None
    try:
        day = datetime.strptime(m.group(1), "%Y%m%d").replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    return day.timestamp() + _DAY


def _committed(j: dict) -> list[tuple[str, dict]]:
    """Committed chains, oldest first by first restore point."""
    out = [(c, e) for c, e in j["chains"].items()
           if isinstance(e, dict) and int(e.get("committed_seq") or 0) >= 1
           and e.get("first_captured_at") is not None]
    out.sort(key=lambda ce: (float(ce[1]["first_captured_at"]), ce[0]))
    return out


def _hold(committed: list[tuple[str, dict]]) -> dict | None:
    if len(committed) < 2:
        return None
    (pc, pe), (nc, ne) = committed[-2], committed[-1]
    prev_b, new_b = int(pe.get("base_bytes") or 0), int(ne.get("base_bytes") or 0)
    if prev_b > 0 and new_b < _HOLD_RATIO * prev_b:
        return {"newest": {"chain": nc, "base_bytes": new_b},
                "previous": {"chain": pc, "base_bytes": prev_b}}
    return None


def _trim_records(j: dict) -> bool:
    """Drop every records entry but the newest from each chain's journal files."""
    changed = False
    for e in j["chains"].values():
        files = e.get("files") if isinstance(e, dict) else None
        if not files:
            continue
        recs = [f for f in files if f.get("kind") == "records"]
        if len(recs) < 2:
            continue
        newest = max(recs, key=lambda f: int(f.get("seq") or 0))
        e["files"] = [f for f in files if f.get("kind") != "records" or f is newest]
        changed = True
    return changed


def _eligible_chains(j: dict, committed, cutoff: float) -> list[str]:
    """Folder ids of journal chains that may go (see module docstring)."""
    out: list[str] = []
    if not committed:
        return out
    # every committed chain but the newest has a committed successor
    for chain, e in committed[:-1]:
        last = e.get("last_captured_at")
        if last is not None and float(last) < cutoff and e.get("folder_id"):
            out.append(e["folder_id"])
    newest_start = float(committed[-1][1]["first_captured_at"])
    names = {c for c, _ in committed}
    for chain, e in j["chains"].items():
        if chain in names or not isinstance(e, dict) or not e.get("folder_id"):
            continue
        if int(e.get("committed_seq") or 0) >= 1:
            continue  # committed but missing first_captured_at: keep
        day_end = _id_day_end(chain)
        if day_end is not None and day_end < cutoff and newest_start > day_end:
            out.append(e["folder_id"])
    return out


def _list(svc, q: str, **extra) -> list[dict]:
    out: list[dict] = []
    token = None
    while True:
        kw = dict(q=q, spaces="drive", corpora="allDrives",
                  includeItemsFromAllDrives=True, supportsAllDrives=True,
                  fields=_LIST_FIELDS, **extra)
        if token:
            kw["pageToken"] = token
        resp = svc.files().list(**kw).execute(num_retries=_NUM_RETRIES) or {}
        out.extend(resp.get("files") or [])
        token = resp.get("nextPageToken")
        if not token or extra.get("pageSize"):
            return out


def _newest_activity(svc, folder: dict) -> float | None:
    """Newest of the folder's own times and its newest child's createdTime."""
    times = [_ts(folder.get("createdTime")), _ts(folder.get("modifiedTime"))]
    kids = _list(svc, f"'{folder['id']}' in parents and trashed = false",
                 orderBy="createdTime desc", pageSize=10)
    times += [_ts(k.get("createdTime")) for k in kids]
    times += [_ts(k.get("modifiedTime")) for k in kids]
    known = [t for t in times if t is not None]
    return max(known) if known else None  # None: no evidence of age, keep


def _unknown_chains(svc, j: dict, loc, committed, cutoff, over) -> list[str]:
    if not committed or not loc.folder_id:
        return []
    newest_start = float(committed[-1][1]["first_captured_at"])
    known = {e.get("folder_id") for e in j["chains"].values() if isinstance(e, dict)}
    folders = _list(svc, f"'{loc.folder_id}' in parents and "
                         f"mimeType = '{backup.FOLDER_MIME}' and trashed = false")
    out = []
    for f in folders:
        if over():
            break
        if not str(f.get("name", "")).startswith("chain-") or f["id"] in known:
            continue
        newest = _newest_activity(svc, f)
        if newest is not None and newest < cutoff and newest_start > newest:
            log.info("backup prune: unknown chain folder %s (%s) last active %s; "
                     "queued", f.get("name"), f["id"], f.get("createdTime"))
            out.append(f["id"])
    return out


def _legacy_files(svc, loc, over) -> list[str]:
    out = []
    for folder in [*loc.legacy_folders, loc.folder_id]:
        if not folder or over():
            continue
        for f in _list(svc, f"'{folder}' in parents and trashed = false"):
            if (f.get("mimeType") != backup.FOLDER_MIME
                    and str(f.get("name", "")).endswith(".enc")):
                out.append(f["id"])
    return out


def _queue(j: dict, ids: list[str]) -> bool:
    added = False
    for fid in ids:
        if fid not in j["pending_deletes"]:
            j["pending_deletes"].append(fid)
            added = True
    return added


def prune_step(home, *, drive_service, retain_days: float, now: float,
               budget_s: float, clock: Callable[[], float] = time.monotonic,
               location=None, resolve=backup_location.resolve) -> dict:
    """One bounded prune pass. Returns {"deleted": [ids], "held": dict | None}."""
    start = clock()

    def over() -> bool:
        return clock() - start >= budget_s

    svc = drive_service
    j = bu.load_journal(home)
    deleted: list[str] = []
    committed = _committed(j)

    held = _hold(committed)
    if held is not None:
        log.warning("backup prune held: newest chain %s base %d bytes is under "
                    "%d%% of previous chain %s (%d bytes); deleting nothing",
                    held["newest"]["chain"], held["newest"]["base_bytes"],
                    int(_HOLD_RATIO * 100), held["previous"]["chain"],
                    held["previous"]["base_bytes"])
        if _trim_records(j):
            bu.save_journal(home, j)
        return {"deleted": deleted, "held": held}

    changed = _trim_records(j)
    if retain_days and retain_days > 0:
        cutoff = now - float(retain_days) * _DAY
        changed |= _queue(j, _eligible_chains(j, committed, cutoff))
        if changed:
            bu.save_journal(home, j)
            changed = False
        loc = location
        if loc is None:
            try:
                loc = resolve(home, svc)
            except Exception as exc:  # noqa: BLE001 — listings are optional
                log.warning("backup prune: cannot resolve the backup location "
                            "(%s); skipping unknown-chain and legacy cleanup", exc)
        if loc is not None and not over():
            try:
                changed |= _queue(j, _unknown_chains(svc, j, loc, committed,
                                                     cutoff, over))
                oldest = (float(committed[0][1]["first_captured_at"])
                          if committed else None)
                if oldest is not None and oldest < cutoff and not over():
                    changed |= _queue(j, _legacy_files(svc, loc, over))
            except Exception as exc:  # noqa: BLE001 — retry next step
                log.warning("backup prune: listing failed (%s); will retry", exc)
    if changed:
        bu.save_journal(home, j)

    by_folder = {e.get("folder_id"): c for c, e in j["chains"].items()
                 if isinstance(e, dict)}
    while j["pending_deletes"] and not over():
        fid = j["pending_deletes"][0]
        try:
            svc.files().delete(fileId=fid, supportsAllDrives=True).execute(
                num_retries=_NUM_RETRIES)
        except Exception as exc:  # noqa: BLE001 — classified below
            if not _is_not_found(exc):
                log.warning("backup prune: could not delete %s (%s); will retry",
                            fid, exc)
                break
        j["pending_deletes"].pop(0)
        chain = by_folder.get(fid)
        if chain is not None:
            j["chains"].pop(chain, None)
            log.info("backup prune: deleted chain %s (folder %s)", chain, fid)
        deleted.append(fid)
        bu.save_journal(home, j)
    return {"deleted": deleted, "held": None}
