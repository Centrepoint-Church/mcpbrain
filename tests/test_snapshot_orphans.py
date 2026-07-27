"""Killed backups leave mcpbrain-snap-* temp dirs; ~24GB accumulated live."""
from mcpbrain import backup


def test_sweep_removes_stale_snapshot_dirs(tmp_path):
    old = tmp_path / "mcpbrain-snap-abc"
    old.mkdir()
    (old / "part.bin").write_bytes(b"x" * 16)
    keep = tmp_path / "unrelated"
    keep.mkdir()
    removed = backup.sweep_orphan_snapshots(tmp_path, max_age_s=0)
    assert removed == 1
    assert not old.exists() and keep.exists()


def test_sweep_spares_recent_dirs(tmp_path):
    fresh = tmp_path / "mcpbrain-snap-def"
    fresh.mkdir()
    assert backup.sweep_orphan_snapshots(tmp_path, max_age_s=3600) == 0
    assert fresh.exists()
