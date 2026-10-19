"""Time-boxed outbox uploader (incremental backup, Task 5).

Real outbox items from `backup_outbox.capture` on tiny SQLite stores; Drive is
the resumable FakeDrive from test_drive_resumable plus a fake `files()` service
over the SAME file store, so manifests and uploaded segments can be read back
and checked byte for byte.
"""
import hashlib
import json
import logging
import os
import re
import shutil
import sqlite3

import pytest

from mcpbrain import backup, backup_outbox as bo, backup_uploader as bu, config
from tests.test_drive_resumable import FakeDrive, FakeTransport

KEY = backup.generate_escrow_key()
FOLDER_MIME = backup.FOLDER_MIME


class _Exec:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, num_retries=0):
        return self._fn()


class Server(FakeDrive):
    """FakeDrive with knobs: corrupt the md5 Drive reports for named files, or
    omit size/md5 from the completion response (as a status query on an
    already-finished session may)."""

    def __init__(self):
        super().__init__()
        self.corrupt: set[str] = set()
        self.bare_final = False
        # files().* state lives on the server so a fresh Service (a new
        # process) sees what an earlier one created
        self.meta: dict[str, dict] = {}   # id -> {name, parents, mimeType}
        self.deleted: list[str] = []
        self.gets: list[str] = []
        self.corrupt_manifest = False
        self.n = 0

    def _final(self, s):
        status, headers, body = super()._final(s)
        meta = json.loads(body)
        if s["meta"]["name"] in self.corrupt:
            meta["md5Checksum"] = "0" * 32
        if self.bare_final:
            meta = {"id": meta["id"]}
        return status, headers, json.dumps(meta).encode()


class Service:
    """googleapiclient-shaped Drive service over `server`'s file store."""

    def __init__(self, server):
        self.server = server
        self.meta, self.deleted, self.gets = server.meta, server.deleted, server.gets

    # every resumable upload's name/parents live on its session
    def _all(self):
        out = dict(self.meta)
        for s in self.server.sessions.values():
            if s["id"] and s["id"] in self.server.files:
                out[s["id"]] = {"name": s["meta"]["name"],
                                "parents": s["meta"]["parents"]}
        return {k: v for k, v in out.items() if k not in self.deleted}

    def by_name(self, name):
        hits = [fid for fid, m in self._all().items() if m["name"] == name]
        return hits

    def drives(self):
        class D:
            def get(self, driveId, **kw):
                return _Exec(lambda: {"id": driveId})
        return D()

    def files(self):
        outer = self

        class F:
            def get(self, fileId, fields=None, supportsAllDrives=None, **kw):
                assert supportsAllDrives is True

                def run():
                    outer.gets.append(fileId)
                    data = outer.server.files[fileId]
                    return {"id": fileId, "size": str(len(data)),
                            "md5Checksum": hashlib.md5(data).hexdigest()}
                return _Exec(run)

            def list(self, q, **kw):
                assert kw.get("supportsAllDrives") is True
                name = re.search(r"name = '([^']*)'", q).group(1)
                parent = re.search(r"'([^']*)' in parents", q).group(1)
                hits = [{"id": i, "name": m["name"]} for i, m in outer._all().items()
                        if m["name"] == name and parent in m["parents"]
                        and m.get("mimeType") == FOLDER_MIME]
                return _Exec(lambda: {"files": hits})

            def create(self, body, media_body=None, supportsAllDrives=None, **kw):
                assert supportsAllDrives is True

                def run():
                    outer.server.n += 1
                    fid = f"svc-{outer.server.n}"
                    outer.meta[fid] = dict(body)
                    if media_body is None:
                        return {"id": fid}
                    data = media_body.getbytes(0, media_body.size())
                    outer.server.files[fid] = data
                    md5 = hashlib.md5(data).hexdigest()
                    if outer.server.corrupt_manifest:
                        md5 = "f" * 32
                    return {"id": fid, "size": str(len(data)), "md5Checksum": md5}
                return _Exec(run)

            def delete(self, fileId, supportsAllDrives=None, **kw):
                assert supportsAllDrives is True

                def run():
                    outer.deleted.append(fileId)
                    outer.server.files.pop(fileId, None)
                    return None
                return _Exec(run)
        return F()


def _make_db(path, rows=200):
    con = sqlite3.connect(path)
    con.execute("PRAGMA page_size=4096")
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
    (records / "a.md").write_text("hello")
    cfg = tmp_path / "config.json"
    cfg.write_text("{}")
    server = Server()

    class E:
        pass

    e = E()
    e.home, e.store, e.records, e.server = home, store, records, server
    e.svc = Service(server)

    def cap(clock=None):
        kw = {"clock": clock} if clock else {}
        return bo.capture(home, store, KEY, records_dir=records, config_path=cfg,
                          settings=bo.Settings(base_threshold=100.0), **kw)

    def step(budget_s=600, transport=None, svc=None, **kw):
        return bu.upload_step(home, drive_service=svc or e.svc,
                              transport=transport or FakeTransport(server),
                              key=KEY, budget_s=budget_s, sleep=lambda s: None, **kw)

    e.cap, e.step = cap, step
    return e


def _manifest(e, chain, seq, tmp_path):
    ids = e.svc.by_name(f"manifest-{chain}-{seq:06d}")
    assert len(ids) == 1, ids
    enc = tmp_path / f"m-{seq}.enc"
    enc.write_bytes(e.server.files[ids[0]])
    out = tmp_path / f"m-{seq}.json"
    backup.decrypt_file(enc, out, KEY)
    return json.loads(out.read_text()), ids[0]


def _state(e):
    return json.loads((e.home / "backup_state.json").read_text())


def test_commits_base_then_delta_with_self_contained_manifests(env, tmp_path):
    base = env.cap()
    _insert(env.store, 50)
    delta = env.cap()
    assert delta.kind == "delta"
    res = env.step()
    assert res.error is None
    assert res.committed == [base.name, delta.name]
    assert res.pending == 0 and res.pending_bytes == 0

    m1, _ = _manifest(env, base.chain, 1, tmp_path)
    m2, m2_id = _manifest(env, base.chain, 2, tmp_path)
    assert m2["format"] == 1 and m2["chain"] == base.chain and m2["seq"] == 2
    assert m2["page_count"] == delta.page_count
    assert m2["db_sha256"] == delta.db_sha256
    assert m2["page_size"] == delta.page_size
    assert m2["captured_at"] == delta.captured_at
    names = [f["name"] for f in m2["files"]]
    want = ([s["name"] for s in base.segments] + [s["name"] for s in delta.segments]
            + [base.records["name"]])
    assert names == want
    assert [f["name"] for f in m1["files"]] == (
        [s["name"] for s in base.segments] + [base.records["name"]])
    for f in m2["files"]:
        data = env.server.files[f["file_id"]]
        assert hashlib.sha256(data).hexdigest() == f["sha256"]
        assert len(data) == f["size"]

    # Drive layout: <person>/<chain>/{segments, manifests}
    person = env.svc.by_name("owner@example.org")
    assert len(person) == 1
    chain_dirs = env.svc.by_name(base.chain)
    assert len(chain_dirs) == 1
    assert env.svc.meta[chain_dirs[0]]["parents"] == [person[0]]
    assert env.svc.meta[m2_id]["parents"] == [chain_dirs[0]]
    seg_id = m2["files"][0]["file_id"]
    assert env.svc._all()[seg_id]["parents"] == [chain_dirs[0]]

    # local commit: sidecars and files gone
    assert bo.pending_items(env.home) == []
    assert not [p for p in bo.outbox_dir(env.home).iterdir()]
    j = bu.load_journal(env.home)
    assert j["chains"][base.chain]["committed_seq"] == 2
    assert j["inflight"] == {}


def test_upload_step_resumes_item_across_uploader_instances(env):
    base = env.cap()
    first = base.segments[0]
    assert first["size"] > 4000

    def drop(n, method, headers, body):
        if method == "PUT" and not headers["Content-Range"].startswith("bytes */"):
            return ("persist", 1000)
        return None

    t1 = FakeTransport(env.server, hook=drop)
    res1 = env.step(budget_s=3, transport=t1)
    assert res1.committed == []
    assert bo.pending_items(env.home)[0].name == base.name
    j = bu.load_journal(env.home)
    assert first["name"] in j["inflight"][base.name]

    # Fresh process: new transport, new service objects, same home.
    t2 = FakeTransport(env.server)
    res2 = env.step(transport=t2, svc=Service(env.server))
    assert res2.error is None
    assert res2.committed == [base.name]
    first_put = next(c for c in t2.calls if c[0] == "PUT")
    assert first_put[2]["Content-Range"] == f"bytes */{first['size']}"
    # The first segment's bytes after the status query: only what Drive lacked.
    uri = first_put[1]
    sent = sum(size for m, u, h, size in t2.calls
               if u == uri and m == "PUT" and not h["Content-Range"].startswith("bytes */"))
    assert sent == first["size"] - 1000
    m, _ = _manifest(env, base.chain, 1, env.home)
    seg = env.server.files[m["files"][0]["file_id"]]
    assert hashlib.sha256(seg).hexdigest() == first["sha256"]


def test_manifest_only_after_every_file_verified(env):
    base = env.cap()
    bad = base.records["name"]
    env.server.corrupt.add(bad)
    res = env.step()
    assert res.error and "md5" in res.error
    assert res.committed == []
    assert not [n for m in env.svc._all().values()
                if (n := m["name"]).startswith("manifest-")]
    assert [it.name for it in bo.pending_items(env.home)] == [base.name]
    assert (bo.outbox_dir(env.home) / base.records["file"]).is_file()
    st = _state(env)
    assert st["consecutive_failures"] == 1 and st["last_success"] is None
    # R10: a real mismatch discards the remote file and restarts it fresh.
    j = bu.load_journal(env.home)
    assert bad not in j["inflight"][base.name]
    bad_ids = [fid for fid in env.svc.deleted]
    assert len(bad_ids) == 1

    env.server.corrupt.clear()
    res2 = env.step()
    assert res2.error is None and res2.committed == [base.name]


def test_coalesces_before_upload_but_not_started_items(env, tmp_path):
    base = env.cap()
    _insert(env.store, 5)
    d2 = env.cap()
    _insert(env.store, 5)
    d3 = env.cap()
    _insert(env.store, 5)
    d4 = env.cap()
    j = bu.load_journal(env.home)
    j["inflight"][d2.name] = {}
    bu.save_journal(env.home, j)
    res = env.step()
    assert res.error is None
    assert res.committed == [base.name, d2.name, d4.name]
    assert d3.name not in res.committed
    m, _ = _manifest(env, base.chain, 4, tmp_path)
    seqs = [f["seq"] for f in m["files"]]
    assert sorted(set(seqs)) == [1, 2, 4]
    assert not env.svc.by_name(f"manifest-{base.chain}-000003")


def test_backup_state_records_restore_point_on_commit(env):
    base = env.cap(clock=lambda: 1_700_000_000.0)
    res = env.step()
    assert res.committed == [base.name]
    st = _state(env)
    assert st["restore_point"] == 1_700_000_000.0
    assert st["last_success"] and st["consecutive_failures"] == 0
    assert st["last_error"] is None


def test_new_chain_base_uploads_into_new_folder(env, tmp_path):
    a = env.cap()
    assert env.step().committed == [a.name]
    # A swapped-in store (restore / optimise_store) has a new inode -> new chain.
    copy = tmp_path / "swap.sqlite3"
    shutil.copy2(env.store, copy)
    os.replace(copy, env.store)
    b = env.cap()
    assert b.kind == "base" and b.chain != a.chain
    res = env.step()
    assert res.committed == [b.name]
    da, db = env.svc.by_name(a.chain), env.svc.by_name(b.chain)
    assert len(da) == 1 and len(db) == 1 and da != db
    m, mid = _manifest(env, b.chain, 1, tmp_path)
    assert env.svc.meta[mid]["parents"] == db
    for f in m["files"]:
        assert env.svc._all()[f["file_id"]]["parents"] == db


def test_failure_keeps_local_files(env):
    base = env.cap()
    before = sorted(p.name for p in bo.outbox_dir(env.home).iterdir())

    def refuse(n, method, headers, body):
        return 403, {}, b'{"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}'

    res = env.step(transport=FakeTransport(env.server, hook=refuse))
    assert res.error
    assert res.committed == []
    assert sorted(p.name for p in bo.outbox_dir(env.home).iterdir()) == before
    assert [it.name for it in bo.pending_items(env.home)] == [base.name]
    assert _state(env)["consecutive_failures"] == 1


def test_gap_in_chain_forces_new_base(env, caplog):
    base = env.cap()
    _insert(env.store, 5)
    d2 = env.cap()
    _insert(env.store, 5)
    d3 = env.cap()
    (bo.outbox_dir(env.home) / f"{d2.name}.json").unlink()
    with caplog.at_level(logging.WARNING, logger="mcpbrain.backup_uploader"):
        res = env.step()
    assert res.committed == [base.name]
    assert res.error is None
    assert bo.pending_items(env.home) == []
    assert not (bo.backup_dir(env.home) / "capture.json").exists()
    gap_logs = [r for r in caplog.records if "gap" in r.getMessage()]
    assert len(gap_logs) == 1
    assert not [p for p in bo.outbox_dir(env.home).iterdir()
                if p.name.startswith(d3.name)]
    _insert(env.store, 5)
    nxt = env.cap()
    assert nxt.kind == "base" and nxt.chain != base.chain


def test_commit_past_capture_state_deletes_capture_json(env):
    """R6: a crash between a sidecar rename and capture.json leaves capture.json
    behind its newest item. Once the uploader deletes that item's sidecar the
    capture could reuse its seq, so the commit deletes capture.json instead."""
    base = env.cap()
    state_path = bo.backup_dir(env.home) / "capture.json"
    stale = state_path.read_text()
    _insert(env.store, 5)
    d2 = env.cap()
    state_path.write_text(stale)          # capture.json still at seq 1
    res = env.step()
    assert res.committed == [base.name, d2.name]
    assert not state_path.exists()
    _insert(env.store, 5)
    assert env.cap().kind == "base"


def test_commit_at_capture_state_keeps_capture_json(env):
    env.cap()
    env.step()
    assert (bo.backup_dir(env.home) / "capture.json").exists()


def test_no_progress_step_records_failure(env):
    """R9: upload_file returns None rather than sleeping past the deadline, so
    a dead link never raises; a step that moved no byte must still say so."""
    base = env.cap()

    def down(n, method, headers, body):
        raise TimeoutError("timed out")

    res = env.step(budget_s=3, transport=FakeTransport(env.server, hook=down))
    assert res.committed == []
    assert res.error and "no progress" in res.error
    st = _state(env)
    assert st["consecutive_failures"] == 1 and "no progress" in st["last_error"]
    j = bu.load_journal(env.home)
    assert list(j["stalls"].values()) == [1]
    env.step(budget_s=3, transport=FakeTransport(env.server, hook=down))
    assert list(bu.load_journal(env.home)["stalls"].values()) == [2]
    assert _state(env)["consecutive_failures"] == 2
    res = env.step()
    assert res.committed == [base.name]
    assert bu.load_journal(env.home)["stalls"] == {}


def test_missing_md5_in_completion_is_reread_not_failed(env):
    """R10: a completion without size/md5Checksum is re-read with files().get."""
    base = env.cap()
    env.server.bare_final = True
    res = env.step()
    assert res.error is None and res.committed == [base.name]
    assert env.svc.gets  # every file re-read
    assert env.svc.deleted == []


def test_corrupt_manifest_is_not_a_commit(env):
    base = env.cap()
    env.server.corrupt_manifest = True
    res = env.step()
    assert res.error and res.committed == []
    assert [it.name for it in bo.pending_items(env.home)] == [base.name]
    assert bu.load_journal(env.home)["chains"][base.chain]["committed_seq"] == 0
    env.server.corrupt_manifest = False
    res = env.step()
    assert res.committed == [base.name]
    # the bad manifest was deleted, exactly one good one remains
    assert len(env.svc.by_name(f"manifest-{base.chain}-000001")) == 1


def test_nothing_pending_makes_no_drive_calls(env):
    class Boom:
        def __getattr__(self, name):
            raise AssertionError("no Drive call expected")

    res = env.step(svc=Boom())
    assert res == bu.StepResult(committed=[], progress=None, error=None,
                                pending=0, pending_bytes=0)


def test_journal_write_is_atomic(env, monkeypatch):
    j = bu.load_journal(env.home)
    j["pending_deletes"] = ["x"]
    bu.save_journal(env.home, j)

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError):
        bu.save_journal(env.home, {"chains": {}, "inflight": {}, "pending_deletes": []})
    monkeypatch.undo()
    assert bu.load_journal(env.home)["pending_deletes"] == ["x"]


def test_on_progress_reports_bytes(env):
    env.cap()
    seen = []
    env.step(on_progress=seen.append)
    assert seen and {"item", "file", "sent", "total"} <= set(seen[-1])



# ---- review fix round 1 -------------------------------------------------

def _flip(path, at=200):
    data = bytearray(path.read_bytes())
    data[at] ^= 0xFF
    path.write_bytes(bytes(data))


def test_crash_after_journal_commit_finishes_cleanup_not_a_gap(env, monkeypatch, caplog):
    """Killed after the journal records committed_seq but before the sidecar
    goes: the next step finishes that commit (restore point, cleanup) and
    uploads the later delta; no gap, no forced base."""
    base = env.cap(clock=lambda: 1_000.0)
    _insert(env.store, 5)
    d2 = env.cap(clock=lambda: 2_000.0)
    _insert(env.store, 5)
    d3 = env.cap(clock=lambda: 3_000.0)
    real = bu._finish_local

    def crash(home, item):
        if item.name == d2.name:
            raise KeyboardInterrupt  # stands in for a kill; not an Exception
        return real(home, item)

    monkeypatch.setattr(bu, "_finish_local", crash)
    j = bu.load_journal(env.home)
    j["inflight"][d3.name] = {}          # keep d3 out of a merge for step 1
    bu.save_journal(env.home, j)
    with pytest.raises(KeyboardInterrupt):
        env.step()
    monkeypatch.setattr(bu, "_finish_local", real)
    assert bu.load_journal(env.home)["chains"][base.chain]["committed_seq"] == 2
    assert d2.name in [it.name for it in bo.pending_items(env.home)]

    with caplog.at_level(logging.WARNING, logger="mcpbrain.backup_uploader"):
        res = env.step()
    assert res.error is None
    assert res.committed == [d2.name, d3.name]
    assert not [r for r in caplog.records if "discarded" in r.getMessage()]
    assert (bo.backup_dir(env.home) / "capture.json").exists()
    st = _state(env)
    assert st["restore_point"] == 3_000.0 and st["consecutive_failures"] == 0
    _insert(env.store, 5)
    assert env.cap().kind == "delta"


def test_committed_item_never_coalesced(env, monkeypatch):
    base = env.cap()
    _insert(env.store, 5)
    d2 = env.cap()
    _insert(env.store, 5)
    d3 = env.cap()
    # journal says d2 is committed, its sidecar survived (crash), d3 not started
    j = bu.load_journal(env.home)
    j["chains"][base.chain] = {"folder_id": "x", "committed_seq": 2, "files": [],
                               "base_bytes": 0, "manifests": []}
    bu.save_journal(env.home, j)
    seen = []
    real = bo.coalesce

    def spy(home, key, *, started, **kw):
        seen.append(set(started))
        return real(home, key, started=started, **kw)

    monkeypatch.setattr(bo, "coalesce", spy)
    monkeypatch.setattr(bu, "_finish_committed", lambda home, j: [])
    bu._coalesce(env.home, KEY, bu.load_journal(env.home), deadline=1e18,
                 clock=lambda: 0.0)
    assert seen and all({base.name, d2.name} <= s for s in seen)
    assert {it.name for it in bo.pending_items(env.home)} == {base.name, d2.name, d3.name}


def test_corrupt_segment_during_coalesce_discards_item_not_uploads(env):
    base = env.cap()
    _insert(env.store, 5)
    d2 = env.cap()
    _insert(env.store, 5)
    env.cap()
    _flip(bo.outbox_dir(env.home) / d2.segments[0]["file"])
    res = env.step()
    assert res.error is None
    assert res.committed == [base.name]
    assert bo.pending_items(env.home) == []
    assert not (bo.backup_dir(env.home) / "capture.json").exists()
    _insert(env.store, 5)
    assert env.cap().kind == "base"


def test_unattributable_coalesce_failure_uploads_unmerged(env, monkeypatch):
    base = env.cap()
    _insert(env.store, 5)
    d2 = env.cap()
    _insert(env.store, 5)
    d3 = env.cap()

    def boom(*a, **kw):
        raise RuntimeError("merge exploded")

    monkeypatch.setattr(bo, "coalesce", boom)
    res = env.step()
    assert res.error is None
    assert res.committed == [base.name, d2.name, d3.name]
    assert (bo.backup_dir(env.home) / "capture.json").exists()


def test_repeated_drive_mismatch_discards_item_after_three(env):
    base = env.cap()
    env.server.corrupt.add(base.segments[0]["name"])
    for n in (1, 2):
        res = env.step()
        assert res.error and "md5" in res.error
        assert [it.name for it in bo.pending_items(env.home)] == [base.name]
        assert (bo.backup_dir(env.home) / "capture.json").exists()
    res = env.step()
    assert bo.pending_items(env.home) == []
    assert not (bo.backup_dir(env.home) / "capture.json").exists()
    assert bu.load_journal(env.home)["mismatches"] == {}


def test_permission_errors_never_discard(env):
    base = env.cap()

    def refuse(n, method, headers, body):
        return 403, {}, b'{"error": {"errors": [{"reason": "insufficientFilePermissions"}]}}'

    for _ in range(5):
        res = env.step(transport=FakeTransport(env.server, hook=refuse))
        assert res.error
    assert [it.name for it in bo.pending_items(env.home)] == [base.name]
    assert (bo.backup_dir(env.home) / "capture.json").exists()
    assert _state(env)["consecutive_failures"] == 5


def test_local_file_not_matching_sidecar_is_never_uploaded(env):
    base = env.cap()
    bad = base.segments[0]
    _flip(bo.outbox_dir(env.home) / bad["file"])
    t = FakeTransport(env.server)
    res = env.step(transport=t)
    assert res.committed == []
    assert not [s for s in env.server.sessions.values()
                if s["meta"]["name"] == bad["name"]]
    assert not [n for m in env.svc._all().values()
                if (n := m["name"]).startswith("manifest-")]
    assert bo.pending_items(env.home) == []
    assert not (bo.backup_dir(env.home) / "capture.json").exists()


def test_coalescing_stays_inside_the_budget(env, monkeypatch):
    env.cap()
    t = [0.0]

    def clock():
        return t[0]

    calls = []

    def slow_merge(home, key, *, started, deadline=None, clock=None):
        calls.append(deadline)
        t[0] += 100           # each merge costs 100 s
        return object()       # "merged something": the loop would go again

    monkeypatch.setattr(bo, "coalesce", slow_merge)
    env.step(budget_s=150, clock=clock)
    assert len(calls) == 2
    assert all(d == 150 for d in calls)
