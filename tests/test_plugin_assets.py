from pathlib import Path
import re
_PLUGIN = Path(__file__).parent.parent / "plugin"
def _read(rel): return (_PLUGIN / rel).read_text()

def test_install_skill_exists():
    assert (_PLUGIN / "skills" / "install" / "SKILL.md").exists()

def test_install_skill_bootstrap_steps():
    b = _read("skills/install/SKILL.md")
    assert "uv tool install" in b and "--python 3.12" in b
    assert "mcpbrain setup" in b and "/reload-plugins" in b

def test_install_skill_vm_sandbox_fallback():
    b = _read("skills/install/SKILL.md")
    assert "Claude Code" in b and ("~/.local" in b or "sandbox" in b.lower())

def test_install_skill_os_detection():
    b = _read("skills/install/SKILL.md")
    assert "launchd" in b.lower() and ("task scheduler" in b.lower() or "schtasks" in b.lower())

def test_install_skill_description_no_angle_brackets():
    b = _read("skills/install/SKILL.md")
    m = re.match(r'^---\n(.*?)\n---', b, re.DOTALL)
    assert m, "must have YAML frontmatter"
    for line in m.group(1).splitlines():
        if line.strip().startswith("description"):
            assert "<" not in line and ">" not in line

def test_backfill_skill_exists():
    assert (_PLUGIN / "skills" / "backfill" / "SKILL.md").exists()

def test_enrich_batch_agent_exists():
    assert (_PLUGIN / "agents" / "enrich-batch.md").exists()

def test_enrich_batch_embeds_rules():
    b = _read("agents/enrich-batch.md")
    for token in ("enrich_queue/pending.json", "enrich_inbox", "batch_id", "content_type", "merge_review"):
        assert token in b

def test_backfill_skill_orchestrates_loop():
    b = _read("skills/backfill/SKILL.md")
    assert "enrich-batch" in b
    assert any(w in b.lower() for w in ("loop", "while", "repeat"))
    assert "pending.json" in b or "spool" in b.lower()
