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
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))  # nothing exists
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    assert cowork_tasks.scheduled_dir() is None


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
