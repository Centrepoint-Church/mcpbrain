"""Records cadences ported into the product: prune + context-health."""
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcpbrain import records, records_cadences


def _repo(tmp_path):
    repo = str(tmp_path / "records")
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")
    return repo


def _hot(repo):
    return Path(repo) / "state" / "hot.md"


def test_prune_drops_entries_older_than_14_days(tmp_path):
    repo = _repo(tmp_path)
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=40)).isoformat()
    recent = (today - timedelta(days=2)).isoformat()
    hot = _hot(repo)
    hot.write_text(
        "# Hot — active continuity\n\n## Just decided\n"
        f"- **{recent}:** keep me\n\n"
        f"- **{old}:** drop me\n"
    )
    removed = records_cadences.prune_hot_md(repo)
    body = hot.read_text()
    assert "keep me" in body
    assert "drop me" not in body
    assert removed >= 1


def test_prune_is_idempotent_no_op_on_fresh(tmp_path):
    repo = _repo(tmp_path)  # scaffold hot.md has no dated entries
    assert records_cadences.prune_hot_md(repo) == 0


def test_records_prune_subcommand_commits(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    today = datetime.now(timezone.utc).date()
    old = (today - timedelta(days=40)).isoformat()
    _hot(repo).write_text(f"# Hot\n\n## Just decided\n- **{old}:** drop me\n")
    subprocess.run(["git", "-C", repo, "commit", "-am", "seed"], check=True, capture_output=True)
    rc = records_cadences.main(["records-prune"])
    assert rc == 0
    log = subprocess.run(
        ["git", "-C", repo, "log", "--oneline"],
        capture_output=True, text=True,
    ).stdout
    assert "prune" in log.lower()


def test_context_health_clean_repo_no_warnings(tmp_path):
    repo = _repo(tmp_path)
    assert records_cadences.context_health(repo, str(tmp_path)) == []


def test_context_health_warns_on_stale_hot_entry(tmp_path):
    repo = _repo(tmp_path)
    old = (datetime.now(timezone.utc).date() - timedelta(days=40)).isoformat()
    _hot(repo).write_text(f"# Hot\n\n## Just decided\n- **{old}:** ancient\n")
    warnings = records_cadences.context_health(repo, str(tmp_path))
    assert any("hot.md" in w for w in warnings)
