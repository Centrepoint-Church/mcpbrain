from datetime import datetime, timedelta, timezone
from pathlib import Path
from mcpbrain import records, records_cadences


def _repo(tmp_path):
    repo = str(tmp_path / "records")
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")
    return repo


def test_multiline_entry_dropped_as_a_unit(tmp_path):
    repo = _repo(tmp_path)
    old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    recent = (datetime.now(timezone.utc).date() - timedelta(days=2)).isoformat()
    hot = Path(repo) / "state" / "hot.md"
    hot.write_text(
        f"# Hot\n\n## Just decided\n"
        f"- **{recent}:** keep\n  with a continuation line\n"
        f"- **{old}:** drop\n  this continuation must go too\n"
    )
    records_cadences.prune_hot_md(repo)
    body = hot.read_text()
    assert "keep" in body and "with a continuation line" in body
    assert "drop" not in body and "this continuation must go too" not in body


def test_dry_run_writes_nothing(tmp_path):
    repo = _repo(tmp_path)
    old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    hot = Path(repo) / "state" / "hot.md"
    hot.write_text(f"# Hot\n\n## Just decided\n- **{old}:** drop\n")
    before = hot.read_text()
    n = records_cadences.prune_hot_md(repo, dry_run=True)
    assert n >= 1 and hot.read_text() == before
