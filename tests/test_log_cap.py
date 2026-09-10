"""mcpbrain/log_cap.py — launchd rotates nothing, so a failure loop fills the disk."""
from mcpbrain.log_cap import cap_agent_logs


def _write(p, n, fill=b"x"):
    p.write_bytes(fill * n)
    return p


def test_caps_an_oversize_log_and_keeps_its_tail(tmp_path):
    p = _write(tmp_path / "com.mcpbrain.err", 5000)
    p.write_bytes(b"o" * 4000 + b"NEWEST-LINES")
    cap_agent_logs(tmp_path, max_bytes=1000, keep_bytes=200)
    after = p.read_bytes()
    assert len(after) < 1000
    assert b"NEWEST-LINES" in after, "the tail is the diagnostic part; it must survive"
    assert b"log capped" in after, "must say it truncated, not silently drop data"


def test_leaves_a_file_under_the_cap_completely_alone(tmp_path):
    p = _write(tmp_path / "com.mcpbrain.log", 500)
    before = p.read_bytes()
    cap_agent_logs(tmp_path, max_bytes=1000, keep_bytes=200)
    assert p.read_bytes() == before


def test_preserves_the_inode_because_launchd_holds_the_file_open(tmp_path):
    """Renaming would leave launchd appending to an invisible old inode forever.
    Truncation keeps the same file; this pins that we never swapped to a rename."""
    p = _write(tmp_path / "com.mcpbrain.err", 5000)
    ino = p.stat().st_ino
    cap_agent_logs(tmp_path, max_bytes=1000, keep_bytes=200)
    assert p.stat().st_ino == ino


def test_covers_both_streams_and_the_records_agents(tmp_path):
    names = ["com.mcpbrain.err", "com.mcpbrain.log",
             "com.mcpbrain.records.prune.err", "com.mcpbrain.tray.err"]
    for n in names:
        _write(tmp_path / n, 5000)
    cap_agent_logs(tmp_path, max_bytes=1000, keep_bytes=200)
    for n in names:
        assert (tmp_path / n).stat().st_size < 1000, n


def test_never_raises_on_an_unreadable_path(tmp_path):
    d = tmp_path / "com.mcpbrain.err"
    d.mkdir()                      # a directory where a file is expected
    assert cap_agent_logs(tmp_path, max_bytes=1) == 0


def test_reports_bytes_freed(tmp_path):
    _write(tmp_path / "com.mcpbrain.err", 100_000)
    assert cap_agent_logs(tmp_path, max_bytes=1000, keep_bytes=200) > 90_000
