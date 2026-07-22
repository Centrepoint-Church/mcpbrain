"""DriveFleetStorage against an in-memory Drive double (no network)."""
import itertools

import httplib2
import pytest
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaInMemoryUpload

from mcpbrain.fleet_storage import DriveFleetStorage
from mcpbrain.org_contracts import FleetStorage

FOLDER_MIME = "application/vnd.google-apps.folder"


class _Req:
    def __init__(self, fn):
        self._fn = fn

    def execute(self, **_kw):
        return self._fn()


class FakeDrive:
    """Minimal Drive files() double: create/list/get_media/update/delete over an
    in-memory node table keyed by id, with (name, parent) lookups."""

    def __init__(self):
        self.nodes = {}   # id -> {id,name,mimeType,parents,data}
        self._ids = ("id%d" % i for i in itertools.count(1))

    def files(self):
        return self

    # -- create ----------------------------------------------------------
    def create(self, body=None, media_body=None, fields=None, supportsAllDrives=None,
               modifiedTime=""):
        def _do():
            # Simulate Drive rejecting a create whose parent no longer
            # exists (e.g. deleted out-of-band since a caller cached its
            # id) -- but only for parents that look like ids this fake
            # itself minted ("id<N>"), so the literal root ids the tests
            # use ("ROOT", "P", "D1", "FLEETFOLDER", ...) are never
            # mistaken for a stale reference.
            for parent in body.get("parents", []):
                if parent.startswith("id") and parent not in self.nodes:
                    raise HttpError(
                        httplib2.Response({"status": 404}),
                        f"File not found: {parent}".encode(),
                    )
            nid = next(self._ids)
            self.nodes[nid] = {
                "id": nid, "name": body["name"],
                "mimeType": body.get("mimeType", "application/octet-stream"),
                "parents": list(body.get("parents", [])),
                "data": media_body.stream().getvalue() if media_body is not None else b"",
                "modifiedTime": modifiedTime,
            }
            return {"id": nid}
        return _Req(_do)

    # -- list ------------------------------------------------------------
    def list(self, q=None, fields=None, pageSize=None, pageToken=None,
             supportsAllDrives=None, includeItemsFromAllDrives=None,
             corpora=None, driveId=None):
        def _do():
            # supports "'<parent>' in parents" + "name = '<n>'" + mimeType filters
            import re
            parent = None
            m = re.search(r"'([^']+)' in parents", q or "")
            if m:
                parent = m.group(1)
            name = None
            m = re.search(r"name\s*=\s*'([^']+)'", q or "")
            if m:
                name = m.group(1)
            want_folder = FOLDER_MIME in (q or "")
            files = []
            # Iterate in a stable order (insertion order of self.nodes) so paging
            # through the same query twice is consistent, mirroring how a real
            # Drive listing is stable across pageToken-driven requests.
            for n in self.nodes.values():
                if parent is not None and parent not in n["parents"]:
                    continue
                if name is not None and n["name"] != name:
                    continue
                if want_folder and n["mimeType"] != FOLDER_MIME:
                    continue
                files.append({"id": n["id"], "name": n["name"],
                              "mimeType": n["mimeType"],
                              "modifiedTime": n.get("modifiedTime", "")})
            # Honor pageSize/pageToken by actually paginating the result set, so
            # callers that don't follow nextPageToken silently see a truncated
            # slice (the bug this test suite guards against).
            start = int(pageToken) if pageToken else 0
            size = pageSize if pageSize else len(files)
            page = files[start:start + size]
            result = {"files": page}
            end = start + size
            # Real Drive only returns nextPageToken if the caller's fields=
            # partial-response mask explicitly requested it — mirror that here
            # so a future edit that accidentally drops "nextPageToken" from a
            # production fields= string is caught by pagination tests going
            # dark, rather than the fake silently keeping them green.
            if end < len(files) and "nextPageToken" in (fields or ""):
                result["nextPageToken"] = str(end)
            return result
        return _Req(_do)

    def get_media(self, fileId=None, supportsAllDrives=None):
        return _Req(lambda: self.nodes[fileId]["data"])

    def update(self, fileId=None, media_body=None, supportsAllDrives=None):
        def _do():
            self.nodes[fileId]["data"] = media_body.stream().getvalue()
            return {"id": fileId}
        return _Req(_do)

    def delete(self, fileId=None, supportsAllDrives=None):
        def _do():
            self.nodes.pop(fileId, None)
            return {}
        return _Req(_do)


def test_satisfies_protocol():
    assert isinstance(DriveFleetStorage(FakeDrive(), "ROOT"), FleetStorage)


def test_put_get_roundtrip_nested_path():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    fs.put_bytes(".mcpbrain-cache/FID.hash.pf.mbc.gz", b"payload")
    assert fs.get_bytes(".mcpbrain-cache/FID.hash.pf.mbc.gz") == b"payload"


def test_get_missing_returns_none():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    assert fs.get_bytes(".mcpbrain-cache/nope.mbc.gz") is None


def test_put_overwrites_existing():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    fs.put_bytes("a/b.bin", b"one")
    fs.put_bytes("a/b.bin", b"two")
    assert fs.get_bytes("a/b.bin") == b"two"
    # only one leaf node named b.bin (update, not duplicate-create)
    leaves = [n for n in drive.nodes.values() if n["name"] == "b.bin"]
    assert len(leaves) == 1


def test_list_paths_by_prefix_sorted():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    fs.put_bytes(".mcpbrain-cache/B.mbc.gz", b"b")
    fs.put_bytes(".mcpbrain-cache/A.mbc.gz", b"a")
    fs.put_bytes("other/x.bin", b"x")
    assert fs.list_paths(".mcpbrain-cache/") == [
        ".mcpbrain-cache/A.mbc.gz", ".mcpbrain-cache/B.mbc.gz"]


def test_delete_is_idempotent():
    fs = DriveFleetStorage(FakeDrive(), "ROOT")
    fs.put_bytes("a/x.bin", b"1")
    fs.delete("a/x.bin")
    fs.delete("a/x.bin")
    assert fs.get_bytes("a/x.bin") is None


def test_list_shared_drives_is_re_exported():
    from mcpbrain import fleet_storage
    from mcpbrain.sync.drive import list_shared_drives as canonical
    assert fleet_storage.list_shared_drives is canonical


def test_fleet_folder_storage_uses_config_folder_id(tmp_path):
    from mcpbrain import config, fleet_storage
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEETFOLDER"}})
    drive = FakeDrive()
    fs = fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=drive)
    assert isinstance(fs, FleetStorage)
    assert fs._root == "FLEETFOLDER"
    # round-trips against the fleet folder root
    fs.put_bytes("org-graph/manifest.json", b'{"version":1}')
    assert fs.get_bytes("org-graph/manifest.json") == b'{"version":1}'


def test_fleet_folder_storage_falls_back_to_org_default(tmp_path):
    from mcpbrain import fleet_storage, org_defaults
    fs = fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=FakeDrive())
    assert fs is not None and fs._root == org_defaults.FLEET_FOLDER_ID


def test_fleet_folder_storage_none_without_service(tmp_path):
    from mcpbrain import config, fleet_storage
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEETFOLDER"}})
    assert fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=None) is None


def test_find_child_paginates_across_multiple_pages():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    # Seed 150 folders sharing one name under ROOT (well past _find_child's
    # pageSize=100), each with a distinct, monotonically-increasing
    # modifiedTime. The tie-break winner is the LAST one created, which lands
    # on the second page — only findable if _find_child follows nextPageToken
    # instead of trusting the first page alone.
    best_id, best_mtime = None, ""
    for i in range(150):
        mtime = f"2024-01-01T{i:04d}Z"
        resp = drive.files().create(
            body={"name": "dup", "mimeType": FOLDER_MIME, "parents": ["P"]},
            modifiedTime=mtime,
        ).execute()
        if mtime > best_mtime:
            best_mtime, best_id = mtime, resp["id"]
    assert fs._find_child("P", "dup", folder=True) == best_id


def test_list_paths_paginates_across_multiple_pages():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    # Seed past list_paths's pageSize=1000 so a single-page fetch would
    # silently truncate results.
    names = [f"file{i:04d}.bin" for i in range(1005)]
    for name in names:
        drive.files().create(body={"name": name, "parents": ["ROOT"]}).execute()
    assert fs.list_paths("") == sorted(names)


def test_ensure_folder_race_converges_to_one_folder():
    drive = FakeDrive()
    fs_a = DriveFleetStorage(drive, "ROOT")
    fs_b = DriveFleetStorage(drive, "ROOT")

    # Simulate the actual find-then-create race: while fs_a's create() call
    # for "shared" is in flight, a second process's create() call for the
    # same name lands concurrently (with a later modifiedTime), producing a
    # genuine duplicate under the same parent *before* fs_a's own create()
    # call returns — exactly the interleaving that makes Drive's lack of
    # name-uniqueness enforcement dangerous. Capture both ids as they're
    # minted (rather than scanning drive.nodes afterwards) because
    # _ensure_folder now opportunistically reaps the loser, so only one of
    # the two will still be present in drive.nodes once it returns.
    real_create = drive.create
    ids = {}

    def racy_create(body=None, media_body=None, fields=None, supportsAllDrives=None,
                     modifiedTime=""):
        if body.get("name") == "shared" and "racer_id" not in ids:
            racer_resp = real_create(
                body={"name": "shared", "mimeType": FOLDER_MIME, "parents": ["ROOT"]},
                modifiedTime="2099-01-01T00:00:00Z",
            ).execute()
            ids["racer_id"] = racer_resp["id"]
        resp = real_create(body=body, media_body=media_body, fields=fields,
                            supportsAllDrives=supportsAllDrives, modifiedTime=modifiedTime).execute()
        ids["own_id"] = resp["id"]
        return _Req(lambda: resp)
    drive.create = racy_create

    fid_a = fs_a._ensure_folder("ROOT", "shared")

    # fs_a must NOT have blindly trusted its own (losing) create() call's id
    # — it has to re-resolve and land on the deterministic tie-break winner
    # (the racer's folder has the later modifiedTime), the same one any
    # other instance would independently compute.
    assert fid_a == ids["racer_id"]
    assert fid_a != ids["own_id"]

    # A second, independent instance (fresh folder cache) resolving the same
    # now-ambiguous name afterwards must converge on the SAME id — no
    # split-brain where different instances write into different duplicates.
    fid_b = fs_b._ensure_folder("ROOT", "shared")
    assert fid_b == fid_a == ids["racer_id"]


def test_ensure_folder_reaps_orphaned_duplicate():
    # Simulates a race's aftermath: two duplicate "shared" folders already
    # exist under the same parent (e.g. left by an earlier out-of-band race)
    # before this instance ever looks. _ensure_folder must both converge on
    # the deterministic winner AND opportunistically delete the loser rather
    # than leaving it behind forever.
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")

    loser_id = drive.create(
        body={"name": "shared", "mimeType": FOLDER_MIME, "parents": ["ROOT"]},
        modifiedTime="2020-01-01T00:00:00Z",
    ).execute()["id"]
    winner_id = drive.create(
        body={"name": "shared", "mimeType": FOLDER_MIME, "parents": ["ROOT"]},
        modifiedTime="2099-01-01T00:00:00Z",
    ).execute()["id"]

    fid = fs._ensure_folder("ROOT", "shared")

    assert fid == winner_id
    assert winner_id in drive.nodes
    assert loser_id not in drive.nodes  # opportunistically reaped


def test_fake_drive_withholds_next_page_token_when_fields_omits_it():
    # Guards the guard: if fields= doesn't literally mention "nextPageToken",
    # FakeDrive.list() must not hand one back even though more pages exist —
    # matching real Drive's partial-response projection semantics. Without
    # this, a future production edit that accidentally drops "nextPageToken"
    # from a fields= mask would silently stop paginating while pagination
    # tests kept passing against the fake.
    drive = FakeDrive()
    for i in range(5):
        drive.files().create(
            body={"name": "dup", "mimeType": FOLDER_MIME, "parents": ["P"]},
        ).execute()
    resp = drive.files().list(
        q="'P' in parents and name = 'dup'",
        fields="files(id,name,mimeType,modifiedTime)",  # deliberately omits nextPageToken
        pageSize=2, pageToken=None, supportsAllDrives=True,
        includeItemsFromAllDrives=True,
    ).execute()
    assert len(resp["files"]) == 2          # truncated — more results exist
    assert "nextPageToken" not in resp      # but withheld since fields= didn't ask for it


def test_list_paths_only_walks_the_resolved_subtree():
    # Proves the efficiency fix: list_paths(".mcpbrain-cache/") must resolve
    # the cache folder first and walk ONLY that subtree, never descending
    # into a large sibling "decoy" subtree living at the drive root. Before
    # the fix, list_paths always walked from self._root, so the decoy
    # folder's id would show up as a queried parent.
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    fs.put_bytes(".mcpbrain-cache/A.mbc.gz", b"a")
    fs.put_bytes(".mcpbrain-cache/B.mbc.gz", b"b")

    # A separate, large decoy subtree at the drive root, NOT under
    # .mcpbrain-cache/.
    decoy_id = drive.create(
        body={"name": "decoy", "mimeType": FOLDER_MIME, "parents": ["ROOT"]},
    ).execute()["id"]
    for i in range(50):
        drive.create(
            body={"name": f"decoy{i}.bin", "parents": [decoy_id]},
        ).execute()

    queried_parents = []
    real_list = drive.list

    def tracking_list(q=None, **kw):
        import re
        m = re.search(r"'([^']+)' in parents", q or "")
        if m:
            queried_parents.append(m.group(1))
        return real_list(q=q, **kw)

    drive.list = tracking_list

    result = fs.list_paths(".mcpbrain-cache/")

    assert result == [".mcpbrain-cache/A.mbc.gz", ".mcpbrain-cache/B.mbc.gz"]
    assert decoy_id not in queried_parents


def test_list_paths_empty_prefix_still_walks_everything():
    # prefix="" must still behave like the old always-walk-from-root code:
    # folder_parts is [], _resolve_parent([], create=False) returns self._root
    # immediately, and everything under root (including files outside any
    # named subfolder) is returned.
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    fs.put_bytes(".mcpbrain-cache/A.mbc.gz", b"a")
    fs.put_bytes("other/x.bin", b"x")
    drive.create(body={"name": "top.bin", "parents": ["ROOT"]}).execute()
    assert fs.list_paths("") == sorted(
        [".mcpbrain-cache/A.mbc.gz", "other/x.bin", "top.bin"]
    )


def test_drive_cache_storage_roots_at_drive_and_uses_cache_prefix(tmp_path):
    from mcpbrain import fleet_storage, ingest_cache
    drive = FakeDrive()
    fs = fleet_storage.drive_cache_storage(drive, "D1")
    assert isinstance(fs, FleetStorage) and fs._root == "D1"
    # ingest_cache addresses the .mcpbrain-cache/ subfolder; no double-prefix
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/FID.h.pf.mbc.gz", b"payload")
    assert fs.get_bytes(f"{ingest_cache.CACHE_DIR}/FID.h.pf.mbc.gz") == b"payload"
    names = [n["name"] for n in drive.nodes.values()]
    assert ingest_cache.CACHE_DIR in names          # exactly one cache folder, at the root


# -- Finding 1: retry/backoff on every Drive call ----------------------------

def test_all_execute_calls_activate_num_retries():
    # Every .execute() this module issues must pass num_retries=5 so the
    # client library's exponential backoff kicks in on transient 5xx/429/
    # quota errors — this is the sole production transport for an
    # unattended daemon. Assert against the fake's recorded kwargs rather
    # than just "it still works", since a bare .execute() would pass this
    # suite too.
    drive = FakeDrive()
    seen_num_retries = []
    real_req_execute = _Req.execute

    def tracking_execute(self, **kw):
        seen_num_retries.append(kw.get("num_retries"))
        return real_req_execute(self, **kw)

    _Req.execute = tracking_execute
    try:
        fs = DriveFleetStorage(drive, "ROOT")
        fs.put_bytes("a/b.bin", b"one")     # create leaf + create folder
        fs.put_bytes("a/b.bin", b"two")     # update leaf
        fs.get_bytes("a/b.bin")             # get_media
        fs.list_paths("a/")                 # walk list
        fs.delete("a/b.bin")                # delete
    finally:
        _Req.execute = real_req_execute

    assert seen_num_retries, "expected at least one tracked .execute() call"
    assert all(n == 5 for n in seen_num_retries), seen_num_retries


# -- Finding 2: concurrent leaf-blob creation converges via read-time tie-break --
# (there's no write-time race-hardening for leaves like _ensure_folder's
# post-create re-resolve for folders -- put_bytes returns None and there's no
# leaf-id cache to converge, so nothing would consult a write-time lookup.
# Convergence instead comes entirely from _find_child's deterministic
# modifiedTime/id tie-break, applied independently by every reader.)

def test_put_bytes_race_converges_to_one_winning_file():
    drive = FakeDrive()
    fs_a = DriveFleetStorage(drive, "ROOT")
    fs_b = DriveFleetStorage(drive, "ROOT")

    # Simulate two installs racing to publish the same brand-new leaf path:
    # while fs_a's create() call for "new.bin" is in flight, a second
    # process's create() call for the same name lands concurrently (with a
    # later modifiedTime) -- Drive enforces no name uniqueness, so both
    # succeed, producing a genuine duplicate blob.
    real_create = drive.create

    def racy_create(body=None, media_body=None, fields=None, supportsAllDrives=None,
                     modifiedTime=""):
        if body.get("name") == "new.bin" and not racy_create.fired:
            racy_create.fired = True
            real_create(
                body={"name": "new.bin", "parents": ["ROOT"]},
                media_body=MediaInMemoryUpload(b"from-race", mimetype="application/octet-stream"),
                modifiedTime="2099-01-01T00:00:00Z",
            ).execute()
        return real_create(body=body, media_body=media_body, fields=fields,
                            supportsAllDrives=supportsAllDrives, modifiedTime=modifiedTime)
    racy_create.fired = False
    drive.create = racy_create

    fs_a.put_bytes("new.bin", b"from-a")

    dups = [n for n in drive.nodes.values() if n["name"] == "new.bin"]
    assert len(dups) == 2  # the race really did create two duplicate blobs
    winner = max(dups, key=lambda n: n["modifiedTime"])

    # Every subsequent reader -- a fresh instance, or fs_a itself -- converges
    # on the SAME winning blob's content via _find_child's deterministic
    # modifiedTime/id tie-break, rather than either racer's own view.
    assert fs_a.get_bytes("new.bin") == winner["data"]
    assert fs_b.get_bytes("new.bin") == winner["data"]


# -- Finding 3: stale _folder_cache entries self-heal ------------------------

def test_put_bytes_self_heals_from_stale_folder_cache():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    fs.put_bytes("a/b/c.bin", b"first")

    b_folder = next(n for n in drive.nodes.values()
                     if n["name"] == "b" and n["mimeType"] == FOLDER_MIME)
    stale_id = b_folder["id"]
    assert stale_id in fs._folder_cache.values()  # sanity: really cached

    # Simulate the cached folder being deleted out-of-band (an admin, or
    # another cleanup process, removes it without this instance's
    # knowledge).
    del drive.nodes[stale_id]

    # A write into the same nested path must self-heal: evict the stale
    # cache entry, re-resolve/re-create "b" fresh, and succeed rather than
    # raising.
    fs.put_bytes("a/b/c2.bin", b"second")

    assert fs.get_bytes("a/b/c2.bin") == b"second"
    assert stale_id not in fs._folder_cache.values()  # cache now points elsewhere


def test_put_bytes_reraises_when_failure_is_not_a_stale_cache_issue():
    # If the HttpError isn't attributable to a cached folder id (e.g. the
    # leaf's parent is the root itself, which is never cached), there is
    # nothing to evict and retry -- the error must propagate rather than
    # being swallowed.
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")

    def failing_create(**kw):
        raise HttpError(httplib2.Response({"status": 500}), b"boom")

    drive.create = failing_create
    with pytest.raises(HttpError):
        fs.put_bytes("top.bin", b"x")  # parent is "ROOT", never a cache value


# -- Finding 6: get_bytes surfaces a non-bytes media response as an error --

def test_get_bytes_raises_on_non_bytes_media_response():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    fs.put_bytes("a.bin", b"real bytes")

    def bogus_get_media(fileId=None, supportsAllDrives=None):
        return _Req(lambda: {"not": "bytes"})

    drive.get_media = bogus_get_media
    with pytest.raises(TypeError):
        fs.get_bytes("a.bin")


# -- Finding 8: _find_child's id tie-break, exercised for real --------------

def test_find_child_tie_break_falls_back_to_highest_id_on_identical_modified_time():
    drive = FakeDrive()
    ids = []
    for _ in range(3):
        resp = drive.create(
            body={"name": "dup", "mimeType": FOLDER_MIME, "parents": ["P"]},
            modifiedTime="2024-01-01T00:00:00Z",  # identical on purpose
        ).execute()
        ids.append(resp["id"])
    expected = max(ids)  # single-digit "id<N>" ids sort lexicographically == numerically

    fs_a = DriveFleetStorage(drive, "ROOT")
    fs_b = DriveFleetStorage(drive, "ROOT")
    assert fs_a._find_child("P", "dup", folder=True) == expected
    assert fs_b._find_child("P", "dup", folder=True) == expected


def test_find_child_tie_break_falls_back_to_highest_id_when_modified_time_both_empty():
    drive = FakeDrive()
    ids = []
    for _ in range(3):
        resp = drive.create(
            body={"name": "dup", "mimeType": FOLDER_MIME, "parents": ["P"]},
            modifiedTime="",  # both/all empty
        ).execute()
        ids.append(resp["id"])
    expected = max(ids)

    fs = DriveFleetStorage(drive, "ROOT")
    assert fs._find_child("P", "dup", folder=True) == expected


# -- Finding 4: _ensure_folder retries the post-create re-resolve -----------

def test_ensure_folder_retries_on_eventual_consistency_then_succeeds():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT", ensure_folder_retry_backoff=0)
    original_find_child = DriveFleetStorage._find_child
    calls = {"n": 0}

    def flaky_find_child(self, parent_id, name, *, folder, reap_duplicates=False):
        calls["n"] += 1
        # Call 1 is the genuine pre-create existence check (must see None).
        # Calls 2 and 3 are forced misses simulating Drive's eventual
        # consistency lag right after create() returns, even though the
        # fake already has the folder. Call 4+ is let through for real and
        # must find it.
        if calls["n"] in (2, 3):
            return None
        return original_find_child(self, parent_id, name, folder=folder,
                                    reap_duplicates=reap_duplicates)

    fs._find_child = flaky_find_child.__get__(fs, DriveFleetStorage)

    fid = fs._ensure_folder("ROOT", "slow")

    assert fid is not None
    assert calls["n"] == 4  # 1 initial + 2 forced-miss retries + 1 real success


def test_ensure_folder_raises_after_retries_exhausted():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT", ensure_folder_retry_attempts=3,
                            ensure_folder_retry_backoff=0)
    original_find_child = DriveFleetStorage._find_child
    calls = {"n": 0}

    def always_missing_after_create(self, parent_id, name, *, folder, reap_duplicates=False):
        calls["n"] += 1
        if calls["n"] == 1:
            # genuine pre-create existence check
            return original_find_child(self, parent_id, name, folder=folder,
                                        reap_duplicates=reap_duplicates)
        return None  # every post-create resolve attempt "never" becomes visible

    fs._find_child = always_missing_after_create.__get__(fs, DriveFleetStorage)

    with pytest.raises(RuntimeError):
        fs._ensure_folder("ROOT", "never-visible")
    # 1 initial check + 3 retry attempts, all forced to miss
    assert calls["n"] == 4


# -- Finding 5: Shared-Drive rooted instances scope their queries -----------

def _tracked_list_calls(drive):
    calls = []
    real_list = drive.list

    def tracking_list(**kw):
        calls.append(kw)
        return real_list(**kw)

    drive.list = tracking_list
    return calls


def test_root_is_drive_scopes_find_child_and_list_paths_queries():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "D1", root_is_drive=True)
    calls = _tracked_list_calls(drive)

    fs.put_bytes("a/b.bin", b"x")
    fs.list_paths("a/")

    assert calls, "expected at least one list() call"
    assert all(c.get("corpora") == "drive" and c.get("driveId") == "D1" for c in calls)


def test_root_is_drive_defaults_false_and_is_unscoped():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    calls = _tracked_list_calls(drive)

    fs.put_bytes("a/b.bin", b"x")
    fs.list_paths("a/")

    assert calls, "expected at least one list() call"
    assert all(c.get("corpora") is None and c.get("driveId") is None for c in calls)


def test_drive_cache_storage_sets_root_is_drive():
    from mcpbrain import fleet_storage
    fs = fleet_storage.drive_cache_storage(FakeDrive(), "D1")
    assert fs._root_is_drive is True


def test_fleet_folder_storage_leaves_root_is_drive_false(tmp_path):
    from mcpbrain import config, fleet_storage
    config.write_config(str(tmp_path), {"fleet": {"folder_id": "FLEETFOLDER"}})
    fs = fleet_storage.fleet_folder_storage(str(tmp_path), drive_service=FakeDrive())
    assert fs._root_is_drive is False


# -- base_path prepending ---------------------------------------------------


def test_base_path_prepends_on_put_and_get():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT", base_path="ingest-cache/D1")
    fs.put_bytes(".mcpbrain-cache/FID.h.pf.mbc.gz", b"payload")
    assert fs.get_bytes(".mcpbrain-cache/FID.h.pf.mbc.gz") == b"payload"
    # physical tree: ROOT > ingest-cache > D1 > .mcpbrain-cache > FID...
    names = {n["name"] for n in drive.nodes.values()}
    assert {"ingest-cache", "D1", ".mcpbrain-cache", "FID.h.pf.mbc.gz"} <= names


def test_base_path_list_paths_returns_caller_relative():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT", base_path="ingest-cache/D1")
    fs.put_bytes(".mcpbrain-cache/A.mbc.gz", b"a")
    fs.put_bytes(".mcpbrain-cache/B.mbc.gz", b"b")
    # base_path must NOT appear in returned paths
    assert fs.list_paths(".mcpbrain-cache/") == [
        ".mcpbrain-cache/A.mbc.gz", ".mcpbrain-cache/B.mbc.gz"]


def test_base_path_isolates_two_source_drives():
    drive = FakeDrive()
    fa = DriveFleetStorage(drive, "ROOT", base_path="ingest-cache/A")
    fb = DriveFleetStorage(drive, "ROOT", base_path="ingest-cache/B")
    fa.put_bytes(".mcpbrain-cache/x.mbc.gz", b"a")
    fb.put_bytes(".mcpbrain-cache/x.mbc.gz", b"b")
    assert fa.get_bytes(".mcpbrain-cache/x.mbc.gz") == b"a"
    assert fb.get_bytes(".mcpbrain-cache/x.mbc.gz") == b"b"
    assert fa.list_paths(".mcpbrain-cache/") == [".mcpbrain-cache/x.mbc.gz"]


def test_base_path_default_is_noop():
    drive = FakeDrive()
    fs = DriveFleetStorage(drive, "ROOT")
    fs.put_bytes(".mcpbrain-cache/x.mbc.gz", b"p")
    assert "ingest-cache" not in {n["name"] for n in drive.nodes.values()}
    assert fs.get_bytes(".mcpbrain-cache/x.mbc.gz") == b"p"


def test_base_path_read_miss_when_base_absent():
    fs = DriveFleetStorage(FakeDrive(), "ROOT", base_path="ingest-cache/D1")
    assert fs.get_bytes(".mcpbrain-cache/nope.mbc.gz") is None
    assert fs.list_paths(".mcpbrain-cache/") == []
