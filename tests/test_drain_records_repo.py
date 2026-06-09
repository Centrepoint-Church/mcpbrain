"""drain._records_repo resolves records_dir and guarantees the repo exists."""
from mcpbrain.drain import _records_repo


def test_records_repo_resolves_and_creates(tmp_path):
    repo = _records_repo(str(tmp_path))
    assert repo == str(tmp_path / "records")
    assert (tmp_path / "records" / ".git").is_dir()
    assert (tmp_path / "records" / "state" / "decisions.md").exists()
