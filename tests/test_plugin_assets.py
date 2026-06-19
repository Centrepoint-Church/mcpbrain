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
    # reliably in the scheduled-task runtime. No stale recurring-task duplicates.
    # (commands/ itself is allowed — it carries the interactive /mcpbrain:install.)
    for n in _RECURRING_ROUTINES:
        assert not (_PLUGIN / "skills" / f"mcpbrain-{n}").exists(), \
            f"mcpbrain-{n} skill should be gone — it's a brain_routine now"
        assert not (_PLUGIN / "commands" / f"{n}.md").exists(), \
            f"{n} command should be gone — it's a brain_routine now"


def test_install_is_a_command():
    # Install is the one flow that must run BEFORE the daemon/MCP exist, so it's a
    # plugin command (prompt-expansion), not MCP-served. It installs the daemon,
    # runs setup, and creates the four Local tasks.
    b = _read("commands/install.md")
    desc = _frontmatter_field(b, "description")
    assert desc and len(desc) <= 200
    assert "uv tool install" in b and "--python 3.12" in b
    assert "mcpbrain setup" in b
    assert "brain_routine" in b
    for t in ("enrich", "meeting-packs", "gardener", "reference-gardener"):
        assert t in b
    assert "Local" in b and "/schedule" in b and "cloud routine" in b.lower()
    assert "restore --auto" not in b and "restore --check" not in b


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

def test_install_doc_points_at_command():
    # INSTALL.md is a short pointer to the /mcpbrain:install command, plus a
    # cold-start path for a machine that doesn't have the plugin yet.
    assert (_PLUGIN / "INSTALL.md").exists()
    assert not (_PLUGIN / "skills" / "mcpbrain-install").exists()  # never a skill
    b = _read("INSTALL.md")
    assert "/mcpbrain:install" in b                         # primary path = the command
    assert "claude plugin install" in b                     # cold-start fallback
    assert "Local" in b and "Cloud routine" in b            # the Local-not-cloud guidance
    # The cowork-setup skill is gone; setup no longer hands off to a Cowork skill.
    assert "mcpbrain-cowork-setup" not in b
    assert not (_PLUGIN / "skills" / "mcpbrain-cowork-setup").exists()

def test_backfill_skill_exists():
    assert (_PLUGIN / "skills" / "mcpbrain-backfill" / "SKILL.md").exists()

def test_enrich_batch_agent_exists():
    assert (_PLUGIN / "agents" / "enrich-batch.md").exists()

def test_enrich_batch_is_unit_worker():
    # The agent is the per-unit work-queue worker: it pulls one unit_id and pushes
    # that unit via MCP. It now EMBEDS the extraction rules in its system prompt (so
    # the rules ride a cacheable prefix shared across the fan-out) and pulls with
    # with_rules=false. The protocol section (above the embedded rules) must still not
    # shell into the spool.
    b = _read("agents/enrich-batch.md")
    for token in ("brain_enrich_pull", "brain_enrich_push", "unit_id", "merge_review",
                  "model: haiku", "with_rules=false"):
        assert token in b, token
    protocol = b[:b.index("<!-- SHARED-EXTRACTION-RULES:BEGIN -->")]
    assert "pending.json" not in protocol   # no direct spool reads in the protocol

def test_backfill_skill_orchestrates_loop():
    b = _read("skills/mcpbrain-backfill/SKILL.md")
    assert "enrich-batch" in b                              # dispatches the unit worker
    assert "brain_enrich_units" in b                        # drains the work-unit queue
    assert any(w in b.lower() for w in ("loop", "while", "repeat"))
    assert "queue" in b.lower()
    # requeue guard: derailed units (no clean status line) get re-dispatched
    assert "requeue" in b.lower() and "derailed" in b.lower()

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
