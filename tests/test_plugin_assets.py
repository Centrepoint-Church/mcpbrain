from pathlib import Path
_PLUGIN = Path(__file__).parent.parent / "plugin"
def _read(rel): return (_PLUGIN / rel).read_text()

def test_install_prompt_doc_exists():
    # Install is distributed as a copy-paste PROMPT (INSTALL.md), not a skill —
    # skill invocation proved unreliable across surfaces; a prompt always works.
    assert (_PLUGIN / "INSTALL.md").exists()
    assert not (_PLUGIN / "skills" / "mcpbrain-install").exists()  # skill removed

def test_install_prompt_has_host_install_and_hands_off_to_cowork():
    b = _read("INSTALL.md")
    assert "uv tool install" in b and "--python 3.12" in b   # the host install
    assert "mcpbrain setup" in b                              # wizard
    assert "restore --auto" in b                              # recovery path
    assert "Claude Code" in b                                 # Part 1 surface
    assert "mcpbrain-cowork-setup" in b                       # ends → Part 2 in Cowork
    assert "Do NOT create any scheduled task or routine here" in b  # no cloud routine

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

def test_cowork_setup_skill_exists():
    assert (_PLUGIN / "skills" / "mcpbrain-cowork-setup" / "SKILL.md").exists()

def test_cowork_setup_creates_local_tasks_not_cloud_routines():
    # Part 2 (Cowork): the four scheduled tasks + project + reload, with an
    # explicit warning that these are LOCAL Cowork tasks, not Claude Code routines.
    b = _read("skills/mcpbrain-cowork-setup/SKILL.md")
    assert "scheduled task" in b.lower() and "hourly" in b.lower()
    for t in ("mcpbrain-enrich", "gardener", "meeting-packs", "reference-gardener"):
        assert t in b
    assert "/reload-plugins" in b
    assert "My Brain" in b                       # creates the project
    assert "cloud routine" in b.lower()          # warns against Claude Code routines
    assert "Cowork" in b
