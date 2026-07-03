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


def test_make_install_isolated_and_configured(tmp_path):
    from tests.helpers.org_fleet import make_install
    from mcpbrain import config
    inst = make_install(tmp_path, "alice", role="member")
    assert inst.home.is_dir()
    assert config.install_role(str(inst.home)) == "member"
    assert config.owner_email(str(inst.home)) == "alice@x.org"
    # store is initialised: entities table + origin column exist
    with inst.store._connect() as db:
        cols = {r["name"] for r in
                db.execute("PRAGMA table_info(entities)").fetchall()}
    assert "origin" in cols


def test_make_fleet_shape(tmp_path):
    from tests.helpers.org_fleet import make_fleet
    from mcpbrain import config
    members, curator, fs = make_fleet(tmp_path, n_members=3)
    assert len(members) == 3
    assert config.is_org_curator(str(curator.home)) is True
    assert all(config.install_role(str(m.home)) == "member" for m in members)
    # the shared fleet storage round-trips for every install
    fs.put_bytes("org-graph/manifest.json", b"{}")
    assert fs.get_bytes("org-graph/manifest.json") == b"{}"
    # installs are isolated: distinct home dirs
    homes = {str(m.home) for m in members} | {str(curator.home)}
    assert len(homes) == 4
