import re
from pathlib import Path
_PLUGIN = Path(__file__).parent.parent / "plugin"
def _read(rel): return (_PLUGIN / rel).read_text()


def _frontmatter_field(text, field):
    # Minimal frontmatter scan (no yaml dep): first `field: value` line.
    m = re.search(rf"^{field}:[ \t]*(.+)$", text, re.MULTILINE)
    return m.group(1).strip() if m else None


_RECURRING_ROUTINES = ["enrich", "meeting-packs", "gardener", "reference-gardener"]


def test_recurring_tasks_have_no_plugin_skill_or_command():
    # The recurring tasks are served via the brain_routine MCP tool (protocols
    # bundled in the wheel) — neither plugin skills nor plugin commands resolve
    # reliably in the scheduled-task runtime. Make sure no stale duplicates linger.
    for n in _RECURRING_ROUTINES:
        assert not (_PLUGIN / "skills" / f"mcpbrain-{n}").exists(), \
            f"mcpbrain-{n} skill should be gone — it's a brain_routine now"
    assert not (_PLUGIN / "commands").exists(), \
        "plugin/commands retired — routines are served via the brain_routine MCP tool"


def test_skill_frontmatter_within_limits():
    # Per the custom-skills spec, name <= 64 and description <= 200 chars. A skill
    # that violates this can be rejected by the loader — and a rejected skill can
    # take discovery down for the whole plugin (which hid mcpbrain-enrich once).
    skills = sorted((_PLUGIN / "skills").glob("*/SKILL.md"))
    assert skills, "no skills found"
    for sk in skills:
        text = sk.read_text()
        name = _frontmatter_field(text, "name")
        desc = _frontmatter_field(text, "description")
        assert name and len(name) <= 64, f"{sk.parent.name}: name missing or >64"
        assert desc and len(desc) <= 200, \
            f"{sk.parent.name}: description {len(desc) if desc else 0} chars (>200)"

def test_install_prompt_doc_exists():
    # Install is distributed as a copy-paste PROMPT (INSTALL.md), not a skill —
    # skill invocation proved unreliable across surfaces; a prompt always works.
    assert (_PLUGIN / "INSTALL.md").exists()
    assert not (_PLUGIN / "skills" / "mcpbrain-install").exists()  # skill removed

def test_install_prompt_is_single_claude_code_flow():
    b = _read("INSTALL.md")
    assert "uv tool install" in b and "--python 3.12" in b   # the host install
    assert "mcpbrain setup" in b                              # wizard
    assert "Claude Code" in b                                 # the one surface
    # All four recurring tasks are created in the same Claude Code flow, each
    # fetching instructions from the brain_routine MCP tool (skills/commands don't
    # resolve reliably in the scheduled-task runtime; MCP tools do).
    assert "brain_routine" in b
    for t in ("enrich", "meeting-packs", "gardener", "reference-gardener"):
        assert t in b
    # …as LOCAL tasks, explicitly NOT cloud routines via /schedule.
    assert "Local" in b and "/schedule" in b and "cloud routine" in b.lower()
    # Backup/restore is automatic — the prompt must NOT tell the user to run it.
    assert "restore --auto" not in b and "restore --check" not in b
    # The cowork-setup skill is gone; setup no longer hands off to a Cowork skill.
    assert "mcpbrain-cowork-setup" not in b
    assert not (_PLUGIN / "skills" / "mcpbrain-cowork-setup").exists()

def test_backfill_skill_exists():
    assert (_PLUGIN / "skills" / "mcpbrain-backfill" / "SKILL.md").exists()

def test_enrich_batch_agent_exists():
    assert (_PLUGIN / "agents" / "enrich-batch.md").exists()

def test_enrich_batch_embeds_rules():
    b = _read("agents/enrich-batch.md")
    for token in ("enrich_queue/pending.json", "enrich_inbox", "batch_id", "content_type", "merge_review"):
        assert token in b

def test_backfill_skill_orchestrates_loop():
    b = _read("skills/mcpbrain-backfill/SKILL.md")
    assert "enrich-batch" in b
    assert any(w in b.lower() for w in ("loop", "while", "repeat"))
    assert "pending.json" in b or "spool" in b.lower()

def test_draft_reply_skill_exists():
    assert (_PLUGIN / "skills" / "mcpbrain-draft-reply" / "SKILL.md").exists()

def test_draft_reply_skill_uses_mcp_tools():
    b = _read("skills/mcpbrain-draft-reply/SKILL.md")
    assert "brain_draft_context" in b
    assert "brain_draft_save" in b
    assert "parent_draft_id" in b   # refinement path documented
    assert len(b) > 1200            # full port, not a stub

def test_draft_reply_skill_names_all_four_stages():
    b = _read("skills/mcpbrain-draft-reply/SKILL.md").lower()
    for stage in ("plan", "draft", "critique", "voice"):
        assert stage in b, f"skill must name the {stage!r} stage"

def test_bootstrap_skill_exists():
    assert (_PLUGIN / "skills" / "mcpbrain-bootstrap" / "SKILL.md").exists()

def test_bootstrap_skill_resolves_home():
    b = _read("skills/mcpbrain-bootstrap/SKILL.md")
    assert "mcpbrain home" in b

def test_bootstrap_skill_targets_corpus_files():
    b = _read("skills/mcpbrain-bootstrap/SKILL.md")
    for f in ("reference/projects.md", "reference/systems.md",
              "reference/org-context.md", "context/preferences.md",
              "context/voice.md"):
        assert f in b, f"bootstrap must write to {f!r}"

def test_cowork_setup_skill_removed():
    # Scheduling now happens in the single Claude Code install prompt (creating
    # Local scheduled tasks), so the separate Cowork-setup skill no longer exists.
    assert not (_PLUGIN / "skills" / "mcpbrain-cowork-setup").exists()
