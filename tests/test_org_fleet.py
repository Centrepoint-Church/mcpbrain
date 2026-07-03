from tests.helpers.org_fleet import LocalDirFleetStorage


def test_put_get_roundtrip(tmp_path):
    fs = LocalDirFleetStorage(tmp_path)
    fs.put_bytes("org-graph/manifest.json", b'{"version":1}')
    assert fs.get_bytes("org-graph/manifest.json") == b'{"version":1}'


def test_get_missing_returns_none(tmp_path):
    fs = LocalDirFleetStorage(tmp_path)
    assert fs.get_bytes("nope/missing.bin") is None


def test_list_paths_by_prefix_sorted(tmp_path):
    fs = LocalDirFleetStorage(tmp_path)
    fs.put_bytes("contrib/b@x.org/2.jsonl", b"b")
    fs.put_bytes("contrib/a@x.org/1.jsonl", b"a")
    fs.put_bytes("org-graph/manifest.json", b"m")
    assert fs.list_paths("contrib/") == [
        "contrib/a@x.org/1.jsonl", "contrib/b@x.org/2.jsonl"]


def test_delete_is_idempotent(tmp_path):
    fs = LocalDirFleetStorage(tmp_path)
    fs.put_bytes("x/y.bin", b"1")
    fs.delete("x/y.bin")
    fs.delete("x/y.bin")   # missing — must not raise
    assert fs.get_bytes("x/y.bin") is None


def test_satisfies_protocol(tmp_path):
    from mcpbrain.org_contracts import FleetStorage
    fs = LocalDirFleetStorage(tmp_path)
    assert isinstance(fs, FleetStorage)   # runtime_checkable structural check
