from pathlib import Path
import re
_PLUGIN = Path(__file__).parent.parent / "plugin"
def _read(rel): return (_PLUGIN / rel).read_text()

def test_install_skill_exists():
    assert (_PLUGIN / "skills" / "mcpbrain-install" / "SKILL.md").exists()

def test_install_skill_bootstrap_steps():
    b = _read("skills/mcpbrain-install/SKILL.md")
    assert "uv tool install" in b and "--python 3.12" in b
    assert "mcpbrain setup" in b and "/reload-plugins" in b

def test_install_skill_runs_in_claude_code():
    # Install runs in Claude Code (host-native); Cowork is the usage surface.
    # No sandbox probe / fallback dance.
    b = _read("skills/mcpbrain-install/SKILL.md")
    assert "Claude Code" in b
    assert "uv tool install" in b                     # the step the Claude Code agent runs
    assert "Cowork" in b                              # usage surface named
    assert "HOST_OK" not in b and "SANDBOX" not in b  # sandbox check removed

def test_install_skill_os_detection():
    b = _read("skills/mcpbrain-install/SKILL.md")
    assert "launchd" in b.lower() and ("task scheduler" in b.lower() or "schtasks" in b.lower())

def test_install_skill_description_no_angle_brackets():
    b = _read("skills/mcpbrain-install/SKILL.md")
    m = re.match(r'^---\n(.*?)\n---', b, re.DOTALL)
    assert m, "must have YAML frontmatter"
    for line in m.group(1).splitlines():
        if line.strip().startswith("description"):
            assert "<" not in line and ">" not in line

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

def test_gardener_skill_exists():
    assert (_PLUGIN / "skills" / "mcpbrain-gardener" / "SKILL.md").exists()

def test_gardener_skill_resolves_home():
    b = _read("skills/mcpbrain-gardener/SKILL.md")
    assert "mcpbrain home" in b

def test_gardener_skill_has_content():
    b = _read("skills/mcpbrain-gardener/SKILL.md")
    assert len(b) > 1500  # full port, not a stub
    assert "MEMORY.md" in b  # key section
    assert "GARDENER-PROTECTED" in b  # protected sections mentioned

def test_meeting_packs_skill_exists():
    assert (_PLUGIN / "skills" / "mcpbrain-meeting-packs" / "SKILL.md").exists()

def test_meeting_packs_skill_uses_host_native_mcp_tools():
    # Routed through MCP (host-native) instead of curl-to-localhost, which the
    # Cowork VM isolates. No shell/curl dependency on the host.
    b = _read("skills/mcpbrain-meeting-packs/SKILL.md")
    assert "brain_meetings_today" in b
    assert "brain_meeting_pack_get" in b
    assert "brain_meeting_pack_upsert" in b

def test_meeting_packs_skill_has_content():
    b = _read("skills/mcpbrain-meeting-packs/SKILL.md")
    assert len(b) > 1500  # full port, not a stub
    assert "context_hash" in b  # change detection
    assert "brain_search" in b  # MCP tool usage

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

def test_reference_gardener_skill_exists():
    assert (_PLUGIN / "skills" / "mcpbrain-reference-gardener" / "SKILL.md").exists()

def test_reference_gardener_skill_resolves_home():
    b = _read("skills/mcpbrain-reference-gardener/SKILL.md")
    assert "mcpbrain home" in b

def test_reference_gardener_skill_propose_not_overwrite():
    b = _read("skills/mcpbrain-reference-gardener/SKILL.md")
    assert "reference/_proposals/" in b  # writes proposals, not directly
    assert "brain_note" in b             # surfaces to owner
    # must not silently overwrite
    assert "must not" in b.lower() or "do not overwrite" in b.lower() or "propose" in b.lower()
    # no-changes stop condition
    assert "no changes to propose" in b.lower() or "nothing" in b.lower()
    # skip rule: don't propose for entries that already match
    assert any(kw in b.lower() for kw in ("skip", "already", "confirms", "without contradiction"))

def test_install_full_autonomous_setup():
    b = _read("skills/mcpbrain-install/SKILL.md")
    assert "scheduled task" in b.lower() and "hourly" in b.lower()
    for t in ("mcpbrain-enrich", "gardener", "meeting-packs", "reference-gardener"):
        assert t in b
    assert "bootstrap" in b.lower()   # runs the interview
    assert "login" in b.lower()       # instruct: open Claude at login
    assert "backup" in b.lower()      # offer Enable backup
