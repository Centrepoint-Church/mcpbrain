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


# ---------------------------------------------------------------------------
# 2c: weekly digest
# ---------------------------------------------------------------------------

def _make_commit(repo, msg):
    subprocess.run(["git", "-C", repo, "commit", "--allow-empty", "-m", msg],
                   check=True, capture_output=True)


def test_build_weekly_digest_includes_tagged_commits(tmp_path):
    repo = _repo(tmp_path)
    _make_commit(repo, "gardener: apply drift (reference/projects.md)")
    _make_commit(repo, "voice: auto-apply voice.md")
    _make_commit(repo, "core: seed core identity")
    _make_commit(repo, "consolidate: graduate theme-meetings")
    digest = records_cadences.build_weekly_digest(repo)
    assert "gardener: apply drift" in digest
    assert "voice: auto-apply voice.md" in digest
    assert "core: seed core identity" in digest
    assert "consolidate: graduate theme-meetings" in digest


def test_build_weekly_digest_excludes_untagged(tmp_path):
    repo = _repo(tmp_path)
    _make_commit(repo, "decision: use X")
    _make_commit(repo, "gardener: apply drift (reference/projects.md)")
    digest = records_cadences.build_weekly_digest(repo)
    assert "gardener: apply drift" in digest
    assert "decision: use X" not in digest


def test_build_weekly_digest_excludes_scaffold_commit(tmp_path):
    repo = _repo(tmp_path)
    # The scaffold commit ("scaffold: initialize records repo") must not appear.
    digest = records_cadences.build_weekly_digest(repo)
    assert "scaffold:" not in digest


def test_build_weekly_digest_empty_when_no_tagged_commits(tmp_path):
    repo = _repo(tmp_path)
    _make_commit(repo, "unrelated: something boring")
    digest = records_cadences.build_weekly_digest(repo)
    assert digest == ""


def test_build_weekly_digest_includes_revert_hint(tmp_path):
    import re as _re
    repo = _repo(tmp_path)
    _make_commit(repo, "gardener: update identity/preferences")
    digest = records_cadences.build_weekly_digest(repo)
    assert "git revert" in digest
    assert _re.search(r"`git revert [0-9a-f]{7}`", digest)


def test_prepend_digest_to_hot_commits_and_appears_in_hot(tmp_path):
    repo = _repo(tmp_path)
    _make_commit(repo, "gardener: apply drift (reference/systems.md)")
    committed = records_cadences.prepend_digest_to_hot(repo)
    assert committed is True
    hot = _hot(repo).read_text()
    assert "Weekly brain digest" in hot
    assert "gardener: apply drift" in hot
    assert "git revert" in hot


def test_prepend_digest_noop_when_no_tagged_commits(tmp_path):
    repo = _repo(tmp_path)
    committed = records_cadences.prepend_digest_to_hot(repo)
    assert committed is False


def test_records_digest_subcommand(tmp_path, monkeypatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    _make_commit(repo, "voice: auto-apply voice.md")
    rc = records_cadences.main(["records-digest"])
    assert rc == 0
    assert "Weekly brain digest" in _hot(repo).read_text()
