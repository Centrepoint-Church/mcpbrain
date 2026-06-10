"""ensure_records_repo: git-inits and scaffolds a local records repo, idempotently."""
from mcpbrain import records
from mcpbrain.records_write import append_decision


def test_creates_repo_and_scaffold(tmp_path):
    repo = str(tmp_path / "records")
    out = records.ensure_records_repo(repo, git_name="t", git_email="t@t")
    assert out == repo
    assert (tmp_path / "records" / ".git").is_dir()
    dec = (tmp_path / "records" / "state" / "decisions.md").read_text()
    assert "Append new decisions at the top. One line per decision." in dec
    hot = (tmp_path / "records" / "state" / "hot.md").read_text()
    assert "## Just decided" in hot
    assert (tmp_path / "records" / "MEMORY.md").exists()
    assert (tmp_path / "records" / "memory").is_dir()
    assert (tmp_path / "records" / "context" / "voice.md").exists()


def test_idempotent_no_clobber_and_writer_appends(tmp_path):
    repo = str(tmp_path / "records")
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")
    # Put custom content in decisions.md, then re-run ensure: must NOT clobber.
    dec_path = tmp_path / "records" / "state" / "decisions.md"
    custom = dec_path.read_text() + "\n| 2026-01-01 | Existing | - | Sam | Active | - |\n"
    dec_path.write_text(custom)
    records.ensure_records_repo(repo, git_name="t", git_email="t@t")
    assert "Existing" in dec_path.read_text()
    # The writer can append + commit against the scaffolded repo.
    assert append_decision(repo, text="Use X", owner="Sam") is True
    assert "| Sam |" in dec_path.read_text()


def test_existing_git_identity_is_not_overridden(tmp_path):
    import subprocess
    repo = str(tmp_path / "records")
    records.ensure_records_repo(repo, git_name="first", git_email="first@x")
    records.ensure_records_repo(repo, git_name="second", git_email="second@x")
    got = subprocess.run(["git", "-C", repo, "config", "user.name"],
                         capture_output=True, text=True).stdout.strip()
    assert got == "first"
