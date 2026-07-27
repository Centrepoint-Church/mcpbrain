"""Killed backups leave mcpbrain-snap-* temp dirs; ~24GB accumulated live."""
import os

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


def test_sweep_does_not_count_a_failed_removal_as_removed(tmp_path, monkeypatch):
    """Review fix: shutil.rmtree is called WITHOUT ignore_errors, so a removal
    that genuinely fails (e.g. a permissions error) raises and is caught by
    the existing `except OSError`, rather than being silently counted as
    removed while the directory is still sitting on disk."""
    old = tmp_path / "mcpbrain-snap-broken"
    old.mkdir()
    (old / "part.bin").write_bytes(b"x" * 16)
    ancient = 0.0
    os.utime(old, (ancient, ancient))

    def _boom(path, *a, **kw):
        raise PermissionError("simulated removal failure")

    monkeypatch.setattr(backup.shutil, "rmtree", _boom)

    removed = backup.sweep_orphan_snapshots(tmp_path, max_age_s=0)

    assert removed == 0, "a failed rmtree must not be counted as removed"
    assert old.exists()
