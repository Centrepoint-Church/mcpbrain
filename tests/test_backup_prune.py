"""Stepwise prune of whole chain folders and legacy .enc files (Task 6).

Drive is a small fake: folders and files with parents/createdTime, a
`files().list` that understands the queries prune issues, and a
`files().delete` that can be told to fail per id. Time is injected (`now`
for retention, `clock` for the budget); home is tmp_path.
"""
import re
from datetime import datetime, timezone

import httplib2
import pytest
from googleapiclient.errors import HttpError

from mcpbrain import backup, backup_prune as bp, backup_uploader as bu
from mcpbrain.backup_location import Location

DAY = 86400.0
NOW = 1_800_000_000.0
FOLDER_MIME = backup.FOLDER_MIME
PERSON = "person-folder"


def _iso(t: float) -> str:
    return datetime.fromtimestamp(t, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S.000Z")


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


class FakeDrive:
    def __init__(self):
        self.items: dict[str, dict] = {}
        self.deleted: list[str] = []
        self.delete_calls: list[str] = []
        self.fail: dict[str, int] = {}
        self.lists: list[dict] = []
        self.kwargs: list[dict] = []

    def add(self, fid, name, parent, *, created, folder=False):
        self.items[fid] = {"id": fid, "name": name, "parents": [parent],
                           "mimeType": FOLDER_MIME if folder else "application/octet-stream",
                           "createdTime": _iso(created), "modifiedTime": _iso(created)}
        return fid

    def files(self):
        return self

    def _alive(self, fid):
        it = self.items.get(fid)
        while it is not None:
            if it["id"] in self.deleted:
                return False
            it = self.items.get(it["parents"][0])
        return fid in self.items

    def list(self, q, **kw):
        assert kw.get("supportsAllDrives") is True
        assert "trashed = false" in q
        self.lists.append({"q": q, **kw})
        parent = re.search(r"'([^']+)' in parents", q).group(1)

        def run():
            out = [dict(it) for fid, it in self.items.items()
                   if it["parents"][0] == parent and self._alive(fid)]
            if f"mimeType = '{FOLDER_MIME}'" in q:
                out = [f for f in out if f["mimeType"] == FOLDER_MIME]
            m = re.search(r"name contains '([^']+)'", q)
            if m:
                out = [f for f in out if m.group(1) in f["name"]]
            if kw.get("orderBy") == "createdTime desc":
                out.sort(key=lambda f: f["createdTime"], reverse=True)
            if kw.get("pageSize"):
                out = out[: kw["pageSize"]]
            return {"files": out}
        return _Exec(run)

    def update(self, fileId, body=None, **kw):
        assert kw.get("supportsAllDrives") is True
        assert body == {"trashed": True}
        self.kwargs.append(kw)

        def run():
            self.delete_calls.append(fileId)
            status = self.fail.get(fileId)
            if status:
                raise HttpError(httplib2.Response({"status": status}), b"{}")
            if fileId not in self.items or fileId in self.deleted:
                raise HttpError(httplib2.Response({"status": 404}), b"{}")
            self.deleted.append(fileId)
            return {"id": fileId, "trashed": True}
        return _Exec(run)


def _loc(legacy=()):
    return Location("drive", "p@example.org", PERSON, tuple(legacy))


def _chain(folder, first, last, *, committed=1, base=1000, files=None):
    return {"folder_id": folder, "committed_seq": committed,
            "files": files or [], "base_bytes": base,
            "first_captured_at": first, "last_captured_at": last,
            "committed_at": last, "manifests": []}


def _journal(home, chains, pending=()):
    j = bu.load_journal(home)
    j["chains"] = chains
    j["pending_deletes"] = list(pending)
    bu.save_journal(home, j)


def _step(home, svc, *, budget_s=60.0, clock=None, loc=None, retain_days=7):
    return bp.prune_step(home, drive_service=svc, retain_days=retain_days, now=NOW,
                         budget_s=budget_s, clock=clock or (lambda: 0.0),
                         location=loc or _loc())


def _drive_with(*folders):
    d = FakeDrive()
    for f in folders:
        d.add(f, f, PERSON, created=NOW - 30 * DAY, folder=True)
    return d


def test_keeps_newest_chain_even_if_old(tmp_path):
    _journal(tmp_path, {"chain-a": _chain("fa", NOW - 40 * DAY, NOW - 30 * DAY)})
    d = _drive_with("fa")
    out = _step(tmp_path, d)
    assert out == {"deleted": [], "held": None}
    assert d.delete_calls == []
    assert "chain-a" in bu.load_journal(tmp_path)["chains"]


def test_deletes_expired_chain_only_after_successor_committed(tmp_path):
    chains = {
        "chain-a": _chain("fa", NOW - 40 * DAY, NOW - 30 * DAY),
        # successor exists but has not committed anything yet
        "chain-b": _chain("fb", None, None, committed=0, base=0),
    }
    _journal(tmp_path, chains)
    d = _drive_with("fa", "fb")
    assert _step(tmp_path, d)["deleted"] == []
    assert d.delete_calls == []

    chains["chain-b"] = _chain("fb", NOW - 2 * DAY, NOW - DAY)
    # a chain inside retention, not the newest, stays too
    chains["chain-m"] = _chain("fm", NOW - 10 * DAY, NOW - 3 * DAY)
    d.add("fm", "chain-m", PERSON, created=NOW - 10 * DAY, folder=True)
    _journal(tmp_path, chains)
    out = _step(tmp_path, d)
    assert out["deleted"] == ["fa"] and out["held"] is None
    j = bu.load_journal(tmp_path)
    assert set(j["chains"]) == {"chain-b", "chain-m"}
    assert j["pending_deletes"] == []


def test_interrupted_prune_continues_from_pending_list(tmp_path):
    chains = {f"chain-{c}": _chain(f"f{c}", NOW - (40 - i) * DAY, NOW - (30 - i) * DAY)
              for i, c in enumerate("abc")}
    chains["chain-z"] = _chain("fz", NOW - DAY, NOW)
    _journal(tmp_path, chains)
    d = _drive_with("fa", "fb", "fc", "fz")
    # the budget runs out as soon as the first delete has been issued
    out = _step(tmp_path, d, budget_s=10,
                clock=lambda: 100.0 if d.delete_calls else 0.0)
    assert out["deleted"] == ["fa"]
    j = bu.load_journal(tmp_path)
    assert j["pending_deletes"] == ["fb", "fc"]
    assert "chain-a" not in j["chains"] and "chain-b" in j["chains"]

    # a new process carries on from the journal's pending list
    out = _step(tmp_path, d)
    assert out["deleted"] == ["fb", "fc"]
    j = bu.load_journal(tmp_path)
    assert j["pending_deletes"] == [] and set(j["chains"]) == {"chain-z"}


def test_small_new_base_holds_prune(tmp_path):
    _journal(tmp_path, {
        "chain-a": _chain("fa", NOW - 40 * DAY, NOW - 30 * DAY, base=1000),
        "chain-b": _chain("fb", NOW - 2 * DAY, NOW, base=400),
    }, pending=["orphan"])
    d = _drive_with("fa", "fb")
    d.add("orphan", "x", "fa", created=NOW)
    d.add("old.enc", "mcpbrain-backup-2026-06-01-0000.enc", PERSON, created=NOW - 120 * DAY)
    out = _step(tmp_path, d)
    assert out["deleted"] == []
    assert out["held"]["newest"]["chain"] == "chain-b"
    assert out["held"]["previous"]["chain"] == "chain-a"
    assert out["held"]["newest"]["base_bytes"] == 400
    assert d.delete_calls == []
    j = bu.load_journal(tmp_path)
    assert set(j["chains"]) == {"chain-a", "chain-b"}


def test_legacy_files_kept_until_new_layout_covers_retention(tmp_path):
    d = _drive_with("fa")
    d.add("legacy", "legacy", "drive", created=NOW - 200 * DAY, folder=True)
    d.add("l1", "mcpbrain-backup-2026-06-01-0000.enc", "legacy", created=NOW - 120 * DAY)
    d.add("l2", "notes.txt", "legacy", created=NOW - 120 * DAY)
    d.add("p1", "snapshot-june.enc", PERSON, created=NOW - 120 * DAY)
    d.add("sub", "keep.enc", "legacy", created=NOW - 120 * DAY, folder=True)
    loc = _loc(legacy=["legacy"])

    # oldest committed restore point is 3 days old: inside retention -> keep
    _journal(tmp_path, {"chain-a": _chain("fa", NOW - 3 * DAY, NOW)})
    assert _step(tmp_path, d, loc=loc)["deleted"] == []
    assert d.delete_calls == []

    # an uncommitted chain alone never covers retention
    _journal(tmp_path, {"chain-a": _chain("fa", NOW - 30 * DAY, None,
                                          committed=0, base=0)})
    assert _step(tmp_path, d, loc=loc)["deleted"] == []

    _journal(tmp_path, {"chain-a": _chain("fa", NOW - 8 * DAY, NOW)})
    out = _step(tmp_path, d, loc=loc)
    assert sorted(out["deleted"]) == ["l1", "p1"]
    assert "l2" not in d.deleted and "sub" not in d.deleted


def test_404_on_delete_is_done(tmp_path):
    _journal(tmp_path, {
        "chain-a": _chain("fa", NOW - 40 * DAY, NOW - 30 * DAY),
        "chain-z": _chain("fz", NOW - DAY, NOW),
    }, pending=["gone", "broken", "later"])
    d = _drive_with("fa", "fz")
    d.add("later", "x", "fz", created=NOW)
    d.fail["broken"] = 500
    out = _step(tmp_path, d)
    # "gone" 404s -> done; "broken" errors -> left, step stops
    assert out["deleted"] == ["gone"]
    j = bu.load_journal(tmp_path)
    assert j["pending_deletes"] == ["broken", "later", "fa"]
    assert "later" not in d.deleted

    d.fail.clear()
    d.fail["fa"] = 404     # someone deleted the folder by hand: still done
    out = _step(tmp_path, d)
    assert out["deleted"] == ["broken", "later", "fa"]
    j = bu.load_journal(tmp_path)
    assert j["pending_deletes"] == [] and set(j["chains"]) == {"chain-z"}


def test_uploader_orphans_on_pending_list_are_drained(tmp_path):
    _journal(tmp_path, {"chain-z": _chain("fz", NOW - DAY, NOW)}, pending=["o1"])
    d = _drive_with("fz")
    d.add("o1", "delta-000002-0000", "fz", created=NOW)
    assert _step(tmp_path, d)["deleted"] == ["o1"]
    assert bu.load_journal(tmp_path)["pending_deletes"] == []


def test_abandoned_uncommitted_chain_deleted_once_newer_chain_commits(tmp_path):
    # chain id dated 20 days before NOW; never committed
    old = datetime.fromtimestamp(NOW - 20 * DAY, tz=timezone.utc).strftime("%Y%m%d")
    young = datetime.fromtimestamp(NOW, tz=timezone.utc).strftime("%Y%m%d")
    chains = {
        f"chain-{old}-aaaaaa": _chain("fo", None, None, committed=0, base=0),
        f"chain-{young}-bbbbbb": _chain("fy", None, None, committed=0, base=0),
        "chain-z": _chain("fz", NOW - 2 * DAY, NOW),
    }
    _journal(tmp_path, chains)
    d = _drive_with("fo", "fy", "fz")
    out = _step(tmp_path, d)
    assert out["deleted"] == ["fo"]
    assert set(bu.load_journal(tmp_path)["chains"]) == {
        f"chain-{young}-bbbbbb", "chain-z"}


def test_unparseable_uncommitted_chain_is_kept(tmp_path):
    _journal(tmp_path, {"weird": _chain("fw", None, None, committed=0, base=0),
                        "chain-z": _chain("fz", NOW - 2 * DAY, NOW)})
    d = _drive_with("fw", "fz")
    assert _step(tmp_path, d)["deleted"] == []


# R15: chain folders the journal does not know -------------------------------

def _unknown(d, fid, newest_file_age_days):
    d.add(fid, f"chain-20260101-{fid}", PERSON, created=NOW - 60 * DAY, folder=True)
    d.add(fid + "-f1", "base-0000", fid, created=NOW - 60 * DAY)
    d.add(fid + "-f2", "delta-000002-0000", fid,
          created=NOW - newest_file_age_days * DAY)


def test_unknown_chain_folders_young_kept_old_deleted(tmp_path):
    _journal(tmp_path, {"chain-z": _chain("fz", NOW - 2 * DAY, NOW)})
    d = _drive_with("fz")
    _unknown(d, "uold", 20)
    _unknown(d, "uyoung", 3)
    d.add("notchain", "other-folder", PERSON, created=NOW - 90 * DAY, folder=True)
    out = _step(tmp_path, d)
    assert out["deleted"] == ["uold"]
    assert "uyoung" not in d.deleted and "notchain" not in d.deleted
    assert "fz" not in d.deleted


def test_unknown_chains_kept_when_no_journal_chain_committed(tmp_path):
    _journal(tmp_path, {"chain-z": _chain("fz", None, None, committed=0, base=0)})
    d = _drive_with("fz")
    _unknown(d, "uold", 20)
    assert _step(tmp_path, d)["deleted"] == []
    _journal(tmp_path, {})
    assert _step(tmp_path, d)["deleted"] == []
    assert d.delete_calls == []


def test_unknown_chain_newer_than_journal_newest_is_kept(tmp_path):
    # journal's newest committed chain started 30 days ago; the unknown folder's
    # newest file is 20 days old -> the unknown one is newer: keep it
    _journal(tmp_path, {"chain-z": _chain("fz", NOW - 30 * DAY, NOW - 25 * DAY)})
    d = _drive_with("fz")
    _unknown(d, "uold", 20)
    assert _step(tmp_path, d)["deleted"] == []


def test_empty_unknown_folder_judged_by_its_own_age(tmp_path):
    _journal(tmp_path, {"chain-z": _chain("fz", NOW - 2 * DAY, NOW)})
    d = _drive_with("fz")
    d.add("e_old", "chain-20260101-eeeeee", PERSON, created=NOW - 30 * DAY, folder=True)
    d.add("e_new", "chain-20260101-ffffff", PERSON, created=NOW - DAY, folder=True)
    assert _step(tmp_path, d)["deleted"] == ["e_old"]


# R16: journal shrinks -------------------------------------------------------

def test_journal_records_entries_trimmed_to_newest(tmp_path):
    files = [{"name": "base-0000", "kind": "base", "seq": 1, "file_id": "b"},
             {"name": "records-000001", "kind": "records", "seq": 1, "file_id": "r1"},
             {"name": "delta-000002-0000", "kind": "delta", "seq": 2, "file_id": "d2"},
             {"name": "records-000002", "kind": "records", "seq": 2, "file_id": "r2"},
             {"name": "records-000003", "kind": "records", "seq": 3, "file_id": "r3"}]
    _journal(tmp_path, {"chain-z": _chain("fz", NOW - DAY, NOW, files=files)})
    d = _drive_with("fz")
    _step(tmp_path, d)
    kept = bu.load_journal(tmp_path)["chains"]["chain-z"]["files"]
    assert [f["file_id"] for f in kept] == ["b", "d2", "r3"]
    assert d.delete_calls == []    # journal only: nothing removed on Drive


def test_no_location_still_prunes_journal_chains(tmp_path):
    _journal(tmp_path, {"chain-a": _chain("fa", NOW - 40 * DAY, NOW - 30 * DAY),
                        "chain-z": _chain("fz", NOW - DAY, NOW)})
    d = _drive_with("fa", "fz")

    def boom(*a, **k):
        raise ValueError("offline")
    out = bp.prune_step(tmp_path, drive_service=d, retain_days=7, now=NOW,
                        budget_s=60, clock=lambda: 0.0, resolve=boom)
    assert out["deleted"] == ["fa"]


@pytest.mark.parametrize("retain", [0, -1])
def test_nonpositive_retention_keeps_everything(tmp_path, retain):
    _journal(tmp_path, {"chain-a": _chain("fa", NOW - 40 * DAY, NOW - 30 * DAY),
                        "chain-z": _chain("fz", NOW - DAY, NOW)})
    d = _drive_with("fa", "fz")
    assert _step(tmp_path, d, retain_days=retain)["deleted"] == []


# R17: trash, and a permanent refusal on one id does not block the rest ------

def test_403_on_one_id_does_not_block_the_next(tmp_path):
    _journal(tmp_path, {"chain-a": _chain("fa", NOW - 40 * DAY, NOW - 30 * DAY),
                        "chain-z": _chain("fz", NOW - DAY, NOW)},
             pending=["stuck", "o2"])
    d = _drive_with("fa", "fz")
    d.add("stuck", "x", "fz", created=NOW)
    d.add("o2", "y", "fz", created=NOW)
    d.fail["stuck"] = 403
    out = _step(tmp_path, d)
    assert out["deleted"] == ["o2", "fa"]
    j = bu.load_journal(tmp_path)
    assert j["pending_deletes"] == ["stuck"]
    assert j["delete_failures"]["stuck"]["count"] == 1
    assert "403" in j["delete_failures"]["stuck"]["error"]
    assert set(j["chains"]) == {"chain-z"}

    _step(tmp_path, d)
    assert bu.load_journal(tmp_path)["delete_failures"]["stuck"]["count"] == 2

    d.fail.clear()           # permission fixed: the next step trashes it
    assert _step(tmp_path, d)["deleted"] == ["stuck"]
    j = bu.load_journal(tmp_path)
    assert j["pending_deletes"] == [] and j["delete_failures"] == {}


@pytest.mark.parametrize("status", [429, 503])
def test_transient_error_stops_the_step(tmp_path, status):
    _journal(tmp_path, {"chain-z": _chain("fz", NOW - DAY, NOW)},
             pending=["busy", "o2"])
    d = _drive_with("fz")
    d.add("busy", "x", "fz", created=NOW)
    d.add("o2", "y", "fz", created=NOW)
    d.fail["busy"] = status
    assert _step(tmp_path, d)["deleted"] == []
    assert d.delete_calls == ["busy"]
    j = bu.load_journal(tmp_path)
    assert j["pending_deletes"] == ["busy", "o2"] and j["delete_failures"] == {}


def test_prune_trashes_rather_than_deletes(tmp_path):
    _journal(tmp_path, {"chain-a": _chain("fa", NOW - 40 * DAY, NOW - 30 * DAY),
                        "chain-z": _chain("fz", NOW - DAY, NOW)})
    d = _drive_with("fa", "fz")
    assert not hasattr(d, "delete")
    assert _step(tmp_path, d)["deleted"] == ["fa"]
