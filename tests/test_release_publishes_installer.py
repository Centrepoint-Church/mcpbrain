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
