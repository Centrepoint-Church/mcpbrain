"""Tests for mcpbrain.draft_critic — S5 voice/coverage/grounding critic.

Acceptance criteria (#21):
  - Critic flags voice violations (em-dash, banned words) — HIGH → revise_needed.
  - composite_confidence penalises violations correctly.
  - critique() never raises even when claude CLI is absent.
  - Flag-off path returns early without calling claude.
  - revise_needed is true when HIGH violation OR grounding_issue present.
"""
from __future__ import annotations

import json
from unittest.mock import patch, MagicMock

import pytest

from mcpbrain.draft_critic import (
    critique,
    composite_confidence,
    _extract_questions,
    _parse_report,
    _fmt_rules,
    _INLINE_VOICE_RULES,
    _EMPTY_REPORT,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_home(tmp_path, **flags):
    (tmp_path / "config.json").write_text(json.dumps(flags))
    return str(tmp_path)


def _make_ctx(subject="Test", body="Can you confirm the deadline?", sender="test@example.com"):
    return {"subject": subject, "body": body, "sender": sender, "voice_rules": ""}


def _mock_report(**overrides):
    base = {
        "voice_violations": [],
        "coverage_issues": [],
        "grounding_issues": [],
        "independent_confidence": 0.8,
        "revise_needed": False,
        "summary": "Looks good.",
    }
    base.update(overrides)
    return json.dumps(base)


# ---------------------------------------------------------------------------
# _extract_questions
# ---------------------------------------------------------------------------

def test_extract_questions_detects_question_mark():
    questions = _extract_questions("Can you confirm the meeting date? Let me know.")
    assert any("confirm" in q.lower() or "?" in q for q in questions)


def test_extract_questions_detects_imperative():
    questions = _extract_questions("Please send the report by Friday.")
    assert len(questions) >= 1


def test_extract_questions_empty_input():
    assert _extract_questions("") == []


# ---------------------------------------------------------------------------
# _fmt_rules
# ---------------------------------------------------------------------------

def test_fmt_rules_contains_severity():
    block = _fmt_rules(_INLINE_VOICE_RULES)
    assert "[HIGH]" in block
    assert "em-dash" in block
    assert "banned-word" in block


# ---------------------------------------------------------------------------
# _parse_report
# ---------------------------------------------------------------------------

def test_parse_report_valid():
    raw = _mock_report()
    report = _parse_report(raw)
    assert report is not None
    assert report["revise_needed"] is False


def test_parse_report_high_violation_sets_revise_needed():
    raw = json.dumps({
        "voice_violations": [{"pattern": "em-dash", "excerpt": "—", "severity": "high"}],
        "coverage_issues": [],
        "grounding_issues": [],
        "independent_confidence": 0.5,
        "revise_needed": False,
        "summary": "",
    })
    report = _parse_report(raw)
    assert report["revise_needed"] is True


def test_parse_report_grounding_issue_sets_revise_needed():
    raw = json.dumps({
        "voice_violations": [],
        "coverage_issues": [],
        "grounding_issues": ["Claim about budget has no support"],
        "independent_confidence": 0.5,
        "revise_needed": False,
        "summary": "",
    })
    report = _parse_report(raw)
    assert report["revise_needed"] is True


def test_parse_report_invalid_json_returns_none():
    assert _parse_report("not json at all") is None


# ---------------------------------------------------------------------------
# composite_confidence
# ---------------------------------------------------------------------------

def test_composite_confidence_no_violations():
    report = {
        "voice_violations": [],
        "coverage_issues": [],
        "grounding_issues": [],
        "independent_confidence": 0.9,
    }
    # 0.7 * 0.8 + 0.3 * 0.9 = 0.56 + 0.27 = 0.83
    conf = composite_confidence(0.8, report)
    assert 0.8 < conf <= 1.0


def test_composite_confidence_high_violation_penalised():
    report = {
        "voice_violations": [{"severity": "high"}],
        "coverage_issues": [],
        "grounding_issues": [],
        "independent_confidence": 0.5,
    }
    no_violation_report = {
        "voice_violations": [],
        "coverage_issues": [],
        "grounding_issues": [],
        "independent_confidence": 0.5,
    }
    conf_with = composite_confidence(0.8, report)
    conf_without = composite_confidence(0.8, no_violation_report)
    assert conf_with < conf_without


def test_composite_confidence_clipped_at_010():
    report = {
        "voice_violations": [{"severity": "high"} for _ in range(20)],
        "coverage_issues": ["issue"] * 20,
        "grounding_issues": ["issue"] * 20,
        "independent_confidence": 0.0,
    }
    conf = composite_confidence(0.0, report)
    assert conf == 0.10


def test_composite_confidence_clipped_at_10():
    report = {
        "voice_violations": [],
        "coverage_issues": [],
        "grounding_issues": [],
        "independent_confidence": 1.0,
    }
    conf = composite_confidence(1.0, report)
    assert conf <= 1.0


# ---------------------------------------------------------------------------
# critique() — integration
# ---------------------------------------------------------------------------

def test_critique_flag_off_returns_empty(tmp_path):
    """When flag is off, critique() returns early without calling claude."""
    home = _make_home(tmp_path, draft_critic=False)
    with patch("mcpbrain.draft_critic._call_claude") as mock_call:
        result = critique("Hello, just checking in.", _make_ctx(), home=home)
    mock_call.assert_not_called()
    assert result["summary"] == "draft_critic_enabled=false"


def test_critique_flag_on_calls_claude(tmp_path):
    """When flag is on, critique() calls the LLM."""
    home = _make_home(tmp_path, draft_critic=True)
    raw = _mock_report()
    with patch("mcpbrain.draft_critic._call_claude", return_value=raw):
        result = critique("Hello.", _make_ctx(), home=home)
    assert result["revise_needed"] is False
    assert isinstance(result["voice_violations"], list)


def test_critique_flags_high_violation(tmp_path):
    """Acceptance: high voice violation → revise_needed=True."""
    home = _make_home(tmp_path, draft_critic=True)
    raw = json.dumps({
        "voice_violations": [{"pattern": "em-dash", "excerpt": "—", "severity": "high"}],
        "coverage_issues": [],
        "grounding_issues": [],
        "independent_confidence": 0.6,
        "revise_needed": False,
        "summary": "Em-dash found.",
    })
    with patch("mcpbrain.draft_critic._call_claude", return_value=raw):
        result = critique("test — test", _make_ctx(), home=home)
    assert result["revise_needed"] is True
    assert any(v.get("pattern") == "em-dash" for v in result["voice_violations"])


def test_critique_flags_grounding_issue(tmp_path):
    """Acceptance: grounding issue → revise_needed=True."""
    home = _make_home(tmp_path, draft_critic=True)
    raw = json.dumps({
        "voice_violations": [],
        "coverage_issues": [],
        "grounding_issues": ["Claims $50k budget but no context supports this"],
        "independent_confidence": 0.7,
        "revise_needed": False,
        "summary": "Ungrounded claim.",
    })
    with patch("mcpbrain.draft_critic._call_claude", return_value=raw):
        result = critique("We have $50k approved.", _make_ctx(), home=home)
    assert result["revise_needed"] is True
    assert len(result["grounding_issues"]) > 0


def test_critique_never_raises(tmp_path):
    """critique() must not raise even when claude CLI fails."""
    home = _make_home(tmp_path, draft_critic=True)
    with patch("mcpbrain.draft_critic._call_claude", side_effect=RuntimeError("broken")):
        result = critique("test", _make_ctx(), home=home)
    assert isinstance(result, dict)
    assert "voice_violations" in result
    assert result["revise_needed"] is False


def test_critique_handles_bad_llm_response(tmp_path):
    """critique() returns empty report if LLM returns garbage."""
    home = _make_home(tmp_path, draft_critic=True)
    with patch("mcpbrain.draft_critic._call_claude", return_value="sorry I cannot help"):
        result = critique("test", _make_ctx(), home=home)
    assert isinstance(result, dict)
    assert result["voice_violations"] == []
