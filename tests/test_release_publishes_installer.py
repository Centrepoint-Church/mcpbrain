"""The published Windows installer must be the one in this repo.

It was hand-copied into mcpbrain-dist at release time with nothing checking it,
so a fixed install.ps1 could sit unpublished indefinitely.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "bin"))
import release  # noqa: E402


def test_copies_the_installer_into_the_dist_root(tmp_path):
    repo, dist = tmp_path / "repo", tmp_path / "dist"
    src = repo / "plugin" / "scripts" / "install.ps1"
    src.parent.mkdir(parents=True)
    src.write_text("# installer v1\n")
    dist.mkdir()

    out = release.copy_installer(repo, dist)

    assert out == dist / "install.ps1"
    assert out.read_text() == "# installer v1\n"


def test_overwrites_a_stale_published_copy(tmp_path):
    repo, dist = tmp_path / "repo", tmp_path / "dist"
    src = repo / "plugin" / "scripts" / "install.ps1"
    src.parent.mkdir(parents=True)
    src.write_text("# installer v2\n")
    dist.mkdir()
    (dist / "install.ps1").write_text("# installer v1\n")

    release.copy_installer(repo, dist)

    assert (dist / "install.ps1").read_text() == "# installer v2\n"


def test_returns_none_when_source_is_absent(tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    assert release.copy_installer(tmp_path / "repo", dist) is None


def test_main_exits_nonzero_when_installer_is_missing(tmp_path, monkeypatch):
    """A scripted release must not report success while quietly skipping the
    Windows installer -- the exact "hand-copied, nothing verifying it" failure
    this whole mechanism exists to close, just moved from "forgot to run the
    cp command" to "the warning scrolled past in CI output"."""
    repo, dist = tmp_path / "repo", tmp_path / "dist"
    repo.mkdir()
    dist.mkdir()
    # No plugin/scripts/install.ps1 under repo -> copy_installer returns None.

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: _FakeCompleted())

    rc = release.main(["--dist", str(dist), "--repo", str(repo)])

    assert rc != 0


def test_main_exits_zero_when_installer_is_published(tmp_path, monkeypatch):
    repo, dist = tmp_path / "repo", tmp_path / "dist"
    (repo / "plugin" / "scripts").mkdir(parents=True)
    (repo / "plugin" / "scripts" / "install.ps1").write_text("# installer\n")
    dist.mkdir()

    class _FakeCompleted:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(release.subprocess, "run", lambda *a, **k: _FakeCompleted())

    rc = release.main(["--dist", str(dist), "--repo", str(repo)])

    assert rc == 0
    assert (dist / "install.ps1").read_text() == "# installer\n"
