"""Resolve the Cowork scheduled-tasks dir and write the enrichment SKILL.md."""
from pathlib import Path

from mcpbrain import cowork_tasks


def test_scheduled_dir_prefers_documents_claude(tmp_path, monkeypatch):
    docs = tmp_path / "Documents" / "Claude"
    docs.mkdir(parents=True)  # parent exists -> Scheduled can be created under it
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cowork_tasks.scheduled_dir() == docs / "Scheduled"


def test_scheduled_dir_none_when_no_parent(tmp_path, monkeypatch):
    assert not (tmp_path / ".claude").exists()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # nothing exists
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cowork_tasks.scheduled_dir() is None


def test_dot_claude_does_not_hijack_cowork(tmp_path, monkeypatch):
    # Regression: a fresh Cowork install where ~/.claude exists (Claude Code CLI)
    # but ~/Documents/Claude doesn't yet. The skill must still target the Cowork
    # dir (which Cowork reads), NOT ~/.claude/scheduled-tasks (which it never scans).
    (tmp_path / ".claude").mkdir()              # exists, but NO scheduled-tasks subdir
    (tmp_path / "Documents").mkdir()            # ~/Documents exists; Claude/ does not
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cowork_tasks.scheduled_dir() == tmp_path / "Documents" / "Claude" / "Scheduled"


def test_uses_code_desktop_only_when_it_already_exists(tmp_path, monkeypatch):
    # If Claude Code Desktop's scheduled-tasks dir actually exists and Cowork's
    # tree does not, fall back to it.
    (tmp_path / ".claude" / "scheduled-tasks").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cowork_tasks.scheduled_dir() == tmp_path / ".claude" / "scheduled-tasks"


def test_write_enrichment_skill_writes_frontmatter_and_body(tmp_path, monkeypatch):
    (tmp_path / "Documents" / "Claude").mkdir(parents=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    p = cowork_tasks.write_enrichment_skill(str(tmp_path / "home"))
    assert p is not None and p.name == "SKILL.md"
    text = p.read_text()
    assert text.startswith("---\nname: mcpbrain-enrichment\n")
    assert "enrich_queue/pending.json" in text  # body present
    assert cowork_tasks.enrichment_skill_present() is True
    # idempotent: second call returns the same path, no crash
    assert cowork_tasks.write_enrichment_skill(str(tmp_path / "home")) == p


def test_write_enrichment_skill_degrades_to_none(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # no Documents/Claude
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cowork_tasks.write_enrichment_skill(str(tmp_path / "home")) is None
