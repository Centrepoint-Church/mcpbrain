import subprocess, pathlib
from mcpbrain import joshbrain_write as jw

def _git(repo, *args): subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True)

def _fake_joshbrain(tmp_path):
    repo = tmp_path / "joshbrain"; repo.mkdir()
    _git(repo, "init"); _git(repo, "config", "user.email", "t@t"); _git(repo, "config", "user.name", "t")
    (repo / "state").mkdir()
    (repo / "state" / "decisions.md").write_text(
        "# Decision Log\n\nAppend new decisions at the top. One line per decision.\n\n")
    (repo / "state" / "hot.md").write_text("# Hot\n\n## Just decided\n\n")
    (repo / "memory").mkdir(); (repo / "MEMORY.md").write_text("# Memory Index\n\n## Project facts\n")
    _git(repo, "add", "-A"); _git(repo, "commit", "-m", "seed")
    return repo

def test_append_decision_inserts_row_and_commits(tmp_path):
    repo = _fake_joshbrain(tmp_path)
    jw.append_decision(str(repo), text="Retire X", rationale="Y", owner="Josh", supersedes="")
    body = (repo / "state" / "decisions.md").read_text()
    assert "Retire X" in body and "| Josh |" in body
    out = subprocess.run(["git", "-C", str(repo), "show", "--stat", "--name-only", "HEAD"],
                         capture_output=True, text=True).stdout
    assert "state/decisions.md" in out

def test_append_continuity_prepends_dated_entry(tmp_path):
    repo = _fake_joshbrain(tmp_path)
    jw.append_continuity(str(repo), text="Shipped parity audit", today="2026-06-23")
    body = (repo / "state" / "hot.md").read_text()
    assert "2026-06-23" in body and "Shipped parity audit" in body

def test_write_memory_creates_file_and_pointer(tmp_path):
    repo = _fake_joshbrain(tmp_path)
    jw.write_memory(str(repo), slug="cowork-traps", description="Cowork gotchas", body="text", memory_type="reference")
    assert (repo / "memory" / "cowork-traps.md").exists()
    assert "cowork-traps" in (repo / "MEMORY.md").read_text()

def test_drain_routes_decision_to_joshbrain(tmp_path):
    repo = _fake_joshbrain(tmp_path)
    home = tmp_path / "mcpbrain_home"; (home / "capture_inbox").mkdir(parents=True)
    from mcpbrain import config, capture, drain
    config.write_config(str(home), {"joshbrain_dir": str(repo)})
    capture.write_capture(str(home), {"kind": "decision", "text": "Routed via drain", "owner": "Josh"})
    drain.drain_captures(store=None, home=str(home))   # store unused for these kinds
    assert "Routed via drain" in (repo / "state" / "decisions.md").read_text()

def test_drain_routes_continuity_to_joshbrain(tmp_path):
    repo = _fake_joshbrain(tmp_path)
    home = tmp_path / "mcpbrain_home"; (home / "capture_inbox").mkdir(parents=True)
    from mcpbrain import config, capture, drain
    config.write_config(str(home), {"joshbrain_dir": str(repo)})
    capture.write_capture(str(home), {"kind": "continuity", "text": "Continuity entry via drain"})
    drain.drain_captures(store=None, home=str(home))
    assert "Continuity entry via drain" in (repo / "state" / "hot.md").read_text()

def test_drain_routes_memory_to_joshbrain(tmp_path):
    repo = _fake_joshbrain(tmp_path)
    home = tmp_path / "mcpbrain_home"; (home / "capture_inbox").mkdir(parents=True)
    from mcpbrain import config, capture, drain
    config.write_config(str(home), {"joshbrain_dir": str(repo)})
    capture.write_capture(str(home), {"kind": "memory", "slug": "test-slug",
                                       "description": "Test memory", "body": "Memory body text"})
    drain.drain_captures(store=None, home=str(home))
    assert (repo / "memory" / "test-slug.md").exists()
    assert "test-slug" in (repo / "MEMORY.md").read_text()
