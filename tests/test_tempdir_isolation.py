"""The suite must never sweep the real OS temp dir.

Daemon.run() and _backup_under_bulk_lock() call
backup.sweep_orphan_snapshots(tempfile.gettempdir(), ...), which rmtree's any
`mcpbrain-snap-*` directory older than the cutoff. Several tests drive a real
Daemon.run(); gettempdir() is /var/folders/... shared with the user's live
daemon, so a planted canary there was deleted by running the suite. A live
backup's work dir is exactly that shape.
"""
import pathlib
import tempfile

from mcpbrain import daemon as d


def test_daemon_sweep_root_is_the_per_test_tmp_dir(tmp_path):
    assert pathlib.Path(d.tempfile.gettempdir()) == tmp_path / "ostmp"


def test_sweep_root_holds_no_real_snapshot_dirs():
    p = pathlib.Path(tempfile.gettempdir())
    assert p.is_dir()
    assert not list(p.glob("mcpbrain-snap-*"))
