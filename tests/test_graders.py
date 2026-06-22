"""Tests for the eval graders (CodeGrader + ModelJudgeGrader).

Keeps graders.py exercised (it's the harness for non-retrieval capability evals,
e.g. the S5 draft critic) rather than dead, untested surface.
"""
from tests.eval.graders import CodeGrader, ModelJudgeGrader


def _registry():
    return {"echo": lambda text="": text}


def test_codegrader_passes_when_assertions_hold():
    g = CodeGrader(tool_registry=_registry())
    case = {
        "id": "echo-pass",
        "tool": "echo",
        "input": {"text": "the budget was approved by the board"},
        "assertions": [
            {"contains": "budget"},
            {"not_contains": "rejected"},
            {"min_length": 5},
        ],
    }
    result = g.run(case)
    assert result.passed
    assert result.failures == []


def test_codegrader_fails_and_reports():
    g = CodeGrader(tool_registry=_registry())
    case = {
        "id": "echo-fail",
        "tool": "echo",
        "input": {"text": "short"},
        "assertions": [{"contains": "budget"}],
    }
    result = g.run(case)
    assert not result.passed
    assert result.failures  # the unmet 'contains' assertion is reported


def test_codegrader_tool_error_is_captured(monkeypatch):
    def boom(**_):
        raise RuntimeError("kaboom")
    g = CodeGrader(tool_registry={"echo": boom})
    case = {"id": "boom", "tool": "echo", "input": {},
            "assertions": [{"contains": "TOOL ERROR"}]}
    result = g.run(case)
    assert result.passed  # the error string is the output, and we asserted on it


def test_modeljudge_skips_cleanly_without_cli(monkeypatch):
    """No claude CLI → SKIP result (passed=True, skipped=True), never raises."""
    import tests.eval.graders as graders
    monkeypatch.setattr(graders, "_claude_cli", lambda: None)
    g = ModelJudgeGrader(tool_registry=_registry())
    result = g.run({"id": "j1", "tool": "echo", "input": {"text": "hi"},
                    "rubric": "Is it friendly?"})
    assert result.passed is True
    assert result.skipped is True
    assert any("SKIP" in r for r in result.judge_reasons)
