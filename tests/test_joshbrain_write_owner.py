"""append_decision records the owner passed by the caller (no Josh default)."""
import subprocess

from mcpbrain.joshbrain_write import append_decision


def _init_repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.email", "t@t"], check=True)
    subprocess.run(["git", "-C", str(tmp_path), "config", "user.name", "t"], check=True)
    (tmp_path / "state").mkdir()
    (tmp_path / "state" / "decisions.md").write_text(
        "# Decisions\n\nAppend new decisions at the top. One line per decision.\n\n"
    )
    subprocess.run(["git", "-C", str(tmp_path), "add", "-A"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(tmp_path), "commit", "-m", "init"],
                   check=True, capture_output=True)


def test_append_decision_records_given_owner(tmp_path):
    _init_repo(tmp_path)
    append_decision(str(tmp_path), text="Adopt X", owner="Sam")
    body = (tmp_path / "state" / "decisions.md").read_text()
    assert "| Sam |" in body
    assert "| Josh |" not in body


def test_append_decision_owner_defaults_empty(tmp_path):
    _init_repo(tmp_path)
    append_decision(str(tmp_path), text="Adopt Y")
    body = (tmp_path / "state" / "decisions.md").read_text()
    assert "| Josh |" not in body
