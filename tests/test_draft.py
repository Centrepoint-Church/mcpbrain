# tests/test_draft.py
import json
import sqlite3
import subprocess
from pathlib import Path
from unittest import mock
import pytest

from mcpbrain import draft as d


class TestFindClaude:
    def test_returns_env_var_if_set(self, monkeypatch, tmp_path):
        fake = str(tmp_path / "fake_claude")
        (tmp_path / "fake_claude").write_text("#!/bin/sh\n")
        monkeypatch.setenv("CLAUDE_BIN", fake)
        assert d._find_claude() == fake

    def test_falls_back_to_local_bin(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        monkeypatch.setattr(d.shutil, "which", lambda x: None)
        fake = tmp_path / ".local" / "bin" / "claude"
        fake.parent.mkdir(parents=True)
        fake.write_text("#!/bin/sh\n")
        monkeypatch.setattr(d.Path, "home", lambda: tmp_path)
        assert d._find_claude() == str(fake)

    def test_raises_if_not_found(self, monkeypatch, tmp_path):
        monkeypatch.delenv("CLAUDE_BIN", raising=False)
        monkeypatch.setattr(d.shutil, "which", lambda x: None)
        monkeypatch.setattr(d.Path, "home", lambda: tmp_path)
        with pytest.raises(RuntimeError, match="claude CLI not found"):
            d._find_claude()


class TestCallLlm:
    def test_returns_stdout_on_success(self, monkeypatch):
        result = mock.MagicMock()
        result.returncode = 0
        result.stdout = "Draft text here"
        monkeypatch.setattr(d.subprocess, "run", lambda *a, **kw: result)
        monkeypatch.setattr(d, "_find_claude", lambda: "/usr/bin/claude")
        out = d._call_llm("Hello")
        assert out == "Draft text here"

    def test_raises_on_nonzero_exit(self, monkeypatch):
        result = mock.MagicMock()
        result.returncode = 1
        result.stderr = "Error: model not found"
        monkeypatch.setattr(d.subprocess, "run", lambda *a, **kw: result)
        monkeypatch.setattr(d, "_find_claude", lambda: "/usr/bin/claude")
        with pytest.raises(RuntimeError, match="claude exited 1"):
            d._call_llm("Hello")

    def test_internal_run_is_isolated(self, monkeypatch):
        # An internal `claude -p` is a tool call, not a user session: it must not
        # fire user hooks (e.g. the SessionEnd capture, which would ingest a junk
        # note per draft) nor spawn the configured MCP servers.
        captured = {}
        result = mock.MagicMock()
        result.returncode = 0
        result.stdout = "ok"

        def fake_run(cmd, *a, **kw):
            captured["cmd"] = cmd
            return result

        monkeypatch.setattr(d.subprocess, "run", fake_run)
        monkeypatch.setattr(d, "_find_claude", lambda: "/usr/bin/claude")
        d._call_llm("Hello")
        cmd = captured["cmd"]
        assert "--strict-mcp-config" in cmd
        assert "--settings" in cmd
        settings = json.loads(cmd[cmd.index("--settings") + 1])
        assert settings.get("disableAllHooks") is True


class TestLoadVoiceRules:
    def test_returns_content_when_file_exists(self, tmp_path):
        jb = tmp_path / "joshbrain" / "context"
        jb.mkdir(parents=True)
        (jb / "voice.md").write_text("Be direct. No em dashes.")
        home = str(tmp_path / ".mcpbrain")
        rules = d._load_voice_rules(home)
        assert "Be direct" in rules

    def test_returns_empty_when_file_missing(self, tmp_path):
        home = str(tmp_path / ".mcpbrain")
        assert d._load_voice_rules(home) == ""


class TestPretrialAndPlan:
    def test_returns_dict_with_intent_and_tone(self, monkeypatch):
        response = json.dumps({
            "intent": "reply",
            "audience_tier": "staff_internal",
            "key_points": ["acknowledge the request", "confirm timeline"],
            "tone_notes": "warm, direct"
        })
        monkeypatch.setattr(d, "_call_llm", lambda prompt, model=None, timeout=None: response)
        result = d.pretrial_and_plan(
            email_subject="Timeline check",
            email_body="Hi Josh, can you confirm when the report is due?",
            sender="Alice",
            voice_rules="Be direct.")
        assert result["intent"] == "reply"
        assert result["audience_tier"] == "staff_internal"
        assert isinstance(result["key_points"], list)

    def test_degrades_on_invalid_json(self, monkeypatch):
        monkeypatch.setattr(d, "_call_llm", lambda prompt, model=None, timeout=None: "not json")
        result = d.pretrial_and_plan("Subj", "Body", "Alice", "")
        assert "intent" in result


class TestGenerateDraft:
    def test_returns_draft_string(self, monkeypatch):
        monkeypatch.setattr(d, "_call_llm",
                            lambda prompt, model=None, timeout=None: "Hi Alice,\n\nReport is due Friday.")
        result = d.generate_draft(
            email_subject="Timeline", email_body="When due?", sender="Alice",
            plan={"intent": "reply", "audience_tier": "staff_internal",
                  "key_points": [], "tone_notes": ""},
            voice_rules="", samples="")
        assert "Friday" in result


class TestCritiqueAndRevise:
    def test_returns_critique_and_revised(self, monkeypatch):
        response = json.dumps({
            "critique": "Good tone, slightly long.",
            "revised_draft": "Hi Alice,\n\nReport due Friday. Let me know if you need it earlier."
        })
        monkeypatch.setattr(d, "_call_llm", lambda prompt, model=None, timeout=None: response)
        result = d.critique_and_revise(
            draft="Hi Alice, the report will be due on Friday of next week.",
            email_subject="Timeline", plan={}, voice_rules="")
        assert "critique" in result
        assert "revised_draft" in result

    def test_degrades_on_invalid_json(self, monkeypatch):
        monkeypatch.setattr(d, "_call_llm", lambda prompt, model=None, timeout=None: "not json")
        result = d.critique_and_revise("original draft", "Subj", {}, "")
        assert result["revised_draft"] == "original draft"


class TestVoiceCheck:
    def test_returns_clean_and_issues(self, monkeypatch):
        response = json.dumps({
            "issues": ["uses em dash on line 2"],
            "clean_draft": "Hi Alice,\n\nReport due Friday."
        })
        monkeypatch.setattr(d, "_call_llm", lambda prompt, model=None, timeout=None: response)
        result = d.voice_check(
            draft="Hi Alice — Report due Friday.",
            voice_rules="No em dashes.")
        assert len(result["issues"]) == 1
        assert "clean_draft" in result

    def test_degrades_on_invalid_json(self, monkeypatch):
        monkeypatch.setattr(d, "_call_llm", lambda prompt, model=None, timeout=None: "plain text")
        result = d.voice_check("plain draft", "")
        assert result["clean_draft"] == "plain draft"


# --- Orchestrator + regression tests (Task E1 review fixes) ------------------

from mcpbrain.store import Store


def _seed_email(store, *, message_id="msg1", subject="Timeline check",
                sender="Alice", sender_email="alice@example.com",
                thread_id="thread1", org="Centrepoint",
                content_type="request",
                summary="Can you confirm when the report is due?",
                reply_needed=1):
    """Insert one email_context row via raw sqlite3 so the orchestrators read it."""
    con = sqlite3.connect(store.path)
    con.execute(
        """INSERT INTO email_context
           (message_id, subject, sender, sender_email, thread_id, org,
            content_type, summary, reply_needed)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (message_id, subject, sender, sender_email, thread_id, org,
         content_type, summary, reply_needed))
    con.commit()
    con.close()


def _fake_llm_factory():
    """Return a _call_llm stand-in that disambiguates the four pipeline calls.

    Pretrial is the only Haiku call; the three Sonnet calls are told apart by
    substrings of their real prompts (confirmed against draft.py):
      - critique_and_revise → "Review this email draft"
      - voice_check         → "Scan this email draft"
      - generate_draft      → everything else
    """
    import json as _j

    def fake(prompt, model=None, timeout=None):
        if model == d._HAIKU:
            return _j.dumps({"intent": "reply", "audience_tier": "staff_internal",
                             "key_points": ["confirm timeline"], "tone_notes": "warm"})
        if "Review this email draft" in prompt:
            return _j.dumps({"critique": "good", "revised_draft": "REVISED BODY"})
        if "Scan this email draft" in prompt:
            return _j.dumps({"issues": [], "clean_draft": "FINAL BODY"})
        return "INITIAL DRAFT BODY"  # generate_draft
    return fake


class TestCallLlmTimeout:
    def test_call_llm_timeout_becomes_runtimeerror(self, monkeypatch):
        def raise_timeout(*a, **kw):
            raise subprocess.TimeoutExpired(cmd="claude", timeout=1)
        monkeypatch.setattr(d, "_find_claude", lambda: "/usr/bin/claude")
        monkeypatch.setattr(d.subprocess, "run", raise_timeout)
        with pytest.raises(RuntimeError, match="timed out"):
            d._call_llm("x")


class TestParseJsonFences:
    def test_parse_json_strips_fences(self, monkeypatch):
        fenced = '```json\n{"critique":"ok","revised_draft":"v2"}\n```'
        monkeypatch.setattr(d, "_call_llm", lambda prompt, model=None, timeout=None: fenced)
        result = d.critique_and_revise(
            draft="original", email_subject="Subj", plan={}, voice_rules="")
        assert result["revised_draft"] == "v2"


class TestDraftEmailEndToEnd:
    def test_draft_email_end_to_end(self, monkeypatch, tmp_path):
        store = Store(tmp_path / "brain.sqlite3", dim=4)
        store.init()
        _seed_email(store, message_id="msg1", thread_id="thread1")
        home = str(tmp_path / ".mcpbrain")
        monkeypatch.setattr(d, "_call_llm", _fake_llm_factory())

        result = d.draft_email(store, home, "msg1")

        assert result["draft_record_id"] > 0
        assert result["final_draft"] == "FINAL BODY"
        assert result["critique"] == "good"
        assert isinstance(result["voice_issues"], list)
        assert result["audience_tier"] == "staff_internal"

        saved = store.get_draft(result["draft_record_id"])
        assert saved is not None
        assert saved["draft_text"] == "FINAL BODY"

    def test_draft_email_raises_on_missing_email(self, tmp_path):
        store = Store(tmp_path / "brain.sqlite3", dim=4)
        store.init()
        home = str(tmp_path / ".mcpbrain")
        with pytest.raises(ValueError):
            d.draft_email(store, home, "nope")


class TestRefineDraft:
    def test_refine_draft_creates_child(self, monkeypatch, tmp_path):
        store = Store(tmp_path / "brain.sqlite3", dim=4)
        store.init()
        home = str(tmp_path / ".mcpbrain")
        parent_id = store.save_draft(
            email_id="msg1", thread_id="thread1", intent="reply",
            audience_tier="staff_internal", draft_text="PARENT BODY",
            critique="", voice_issues=[], samples_used=0, model=d._SONNET)
        monkeypatch.setattr(d, "_call_llm", _fake_llm_factory())

        result = d.refine_draft(store, home, parent_id, "warmer")

        child_id = result["draft_record_id"]
        assert child_id != parent_id
        child = store.get_draft(child_id)
        assert child["parent_draft_id"] == parent_id
        assert child["refinement"] == "warmer"

    def test_refine_draft_raises_on_missing_parent(self, tmp_path):
        store = Store(tmp_path / "brain.sqlite3", dim=4)
        store.init()
        home = str(tmp_path / ".mcpbrain")
        with pytest.raises(ValueError):
            d.refine_draft(store, home, 9999, "warmer")
