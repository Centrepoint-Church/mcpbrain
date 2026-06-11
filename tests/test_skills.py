"""Personal skills are written to ~/.claude/skills with correct front-matter."""
from pathlib import Path

from mcpbrain import skills


def test_skills_dir_honours_config_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "cfg"))
    assert skills.skills_dir() == tmp_path / "cfg" / "skills"
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert skills.skills_dir() == tmp_path / ".claude" / "skills"


def test_write_personal_skills_writes_both(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    paths = skills.write_personal_skills()
    assert len(paths) == 2
    enr = tmp_path / ".claude" / "skills" / "mcpbrain-enrichment" / "SKILL.md"
    setup = tmp_path / ".claude" / "skills" / "mcpbrain-setup" / "SKILL.md"
    assert enr.exists() and setup.exists()
    enr_text = enr.read_text()
    assert enr_text.startswith("---\nname: mcpbrain-enrichment\n")
    assert "enrich_queue/pending.json" in enr_text  # body from cowork/enrichment.md
    setup_text = setup.read_text()
    assert setup_text.startswith("---\nname: mcpbrain-setup\n")
    assert "hourly" in setup_text.lower()
    assert skills.enrichment_skill_present() and skills.setup_skill_present()


def test_write_personal_skills_idempotent(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    a = skills.write_personal_skills()
    b = skills.write_personal_skills()
    assert a == b and len(b) == 2


def test_enrichment_body_matches_package_data(tmp_path, monkeypatch):
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    skills.write_personal_skills()
    enr = (tmp_path / ".claude" / "skills" / "mcpbrain-enrichment" / "SKILL.md").read_text()
    canonical = (Path(skills.__file__).parent / "cowork" / "enrichment.md").read_text()
    assert canonical in enr  # the packaged body is embedded verbatim after front-matter


def test_skill_descriptions_have_no_xml_tags(tmp_path, monkeypatch):
    # Cowork rejects a SKILL.md whose YAML `description:` contains angle brackets.
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    skills.write_personal_skills()
    for name in ("mcpbrain-enrichment", "mcpbrain-setup"):
        text = (tmp_path / ".claude" / "skills" / name / "SKILL.md").read_text()
        # the front-matter description line is line 3 (---\nname:\ndescription:)
        desc = [ln for ln in text.splitlines() if ln.startswith("description:")][0]
        assert "<" not in desc and ">" not in desc, f"{name} description has a tag: {desc}"
