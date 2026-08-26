"""The connector write must merge, never replace, and must be atomic.

~/.claude.json carries the user's whole project history; claude_desktop_config.json
carries their Cowork preferences alongside mcpServers. Both have been destroyed in
the wild by tools that rewrote them wholesale.
"""
import json

from mcpbrain import connector


def test_server_entry_shapes():
    plain = connector.server_entry("/abs/bin/mcpbrain", typed=False)
    assert plain == {"command": "/abs/bin/mcpbrain", "args": ["mcp-server"]}
    typed = connector.server_entry("/abs/bin/mcpbrain", typed=True)
    assert typed == {"type": "stdio", "command": "/abs/bin/mcpbrain",
                     "args": ["mcp-server"], "env": {}}


def test_merge_preserves_every_other_key(tmp_path):
    cfg = tmp_path / "claude_desktop_config.json"
    cfg.write_text(json.dumps({
        "mcpServers": {"other": {"command": "x"}},
        "preferences": {"menuBarEnabled": True},
        "coworkUserFilesPath": "/Users/x/Claude",
    }))
    ok, _, _ = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=True)
    assert ok
    data = json.loads(cfg.read_text())
    assert data["preferences"] == {"menuBarEnabled": True}
    assert data["coworkUserFilesPath"] == "/Users/x/Claude"
    assert data["mcpServers"]["other"] == {"command": "x"}
    assert data["mcpServers"]["mcpbrain"]["command"] == "/abs/bin/mcpbrain"


def test_merge_is_idempotent(tmp_path):
    cfg = tmp_path / "c.json"
    entry = connector.server_entry("/abs/bin/mcpbrain", typed=False)
    connector.merge_server_into(cfg, entry, create=True)
    first = cfg.read_text()
    connector.merge_server_into(cfg, entry, create=True)
    assert cfg.read_text() == first


def test_unparseable_file_is_left_byte_identical(tmp_path):
    cfg = tmp_path / "c.json"
    cfg.write_text("{ this is not json")
    ok, status, detail = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=True)
    assert ok is False
    assert "parse" in detail.lower()
    assert cfg.read_text() == "{ this is not json"


def test_missing_file_is_skipped_when_create_is_false(tmp_path):
    cfg = tmp_path / "nope.json"
    ok, status, detail = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=False)
    assert ok is True and status == connector.STATUS_SKIPPED
    assert "not present" in detail.lower()
    assert not cfg.exists()


def test_non_dict_top_level_is_refused(tmp_path):
    # A JSON array parses fine but is not a config; overwriting it would destroy
    # whatever it is. Refuse rather than replace.
    cfg = tmp_path / "c.json"
    cfg.write_text("[1, 2, 3]")
    ok, _, _ = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=True)
    assert ok is False
    assert cfg.read_text() == "[1, 2, 3]"


def test_non_dict_mcp_servers_value_is_refused(tmp_path):
    # A non-dict value AT the mcpServers key (e.g. a stray list) parses fine as
    # an object at the top level, but overwriting it wholesale would discard
    # whatever it actually is. Refuse rather than replace, same as a non-dict
    # top level.
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"mcpServers": [1, 2]}))
    ok, status, detail = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=True)
    assert ok is False
    assert "mcpServers" in detail
    assert cfg.read_text() == json.dumps({"mcpServers": [1, 2]})


def test_write_is_atomic_no_partial_file(tmp_path, monkeypatch):
    # If os.replace fails, the original must survive intact.
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"keep": 1}))

    def boom(src, dst):
        raise OSError("disk full")

    monkeypatch.setattr(connector.os, "replace", boom)
    ok, _, _ = connector.merge_server_into(
        cfg, connector.server_entry("/abs/bin/mcpbrain", typed=False), create=True)
    assert ok is False
    assert json.loads(cfg.read_text()) == {"keep": 1}
    assert list(tmp_path.glob("*.tmp")) == []   # temp file cleaned up


def test_concurrent_calls_use_unique_tmp_filenames(tmp_path, monkeypatch):
    # Concurrent calls to merge_server_into targeting the same file must not
    # collide on a shared tmp filename. Capture the tmp paths used by monkeypatching
    # tempfile.mkstemp to track which temp files are created.
    cfg = tmp_path / "c.json"

    used_tmp_paths = []

    original_mkstemp = connector.tempfile.mkstemp

    def tracking_mkstemp(*args, **kwargs):
        fd, path_str = original_mkstemp(*args, **kwargs)
        used_tmp_paths.append(path_str)
        return fd, path_str

    monkeypatch.setattr(connector.tempfile, "mkstemp", tracking_mkstemp)

    # Make two back-to-back calls with different entries so both must write
    # and both call mkstemp
    entry1 = connector.server_entry("/abs/bin/mcpbrain-v1", typed=False)
    connector.merge_server_into(cfg, entry1, create=True)

    entry2 = connector.server_entry("/abs/bin/mcpbrain-v2", typed=False)
    connector.merge_server_into(cfg, entry2, create=True)

    # Verify at least two tmp files were used
    assert len(used_tmp_paths) >= 2, f"Expected at least 2 tmp calls, got {len(used_tmp_paths)}"
    # Verify they are different (not reusing a fixed tmp filename)
    assert used_tmp_paths[0] != used_tmp_paths[1], \
        f"Tmp filenames must be unique; both calls used {used_tmp_paths[0]}"
