"""generate_draft writes the reply from the configured owner, not 'Sam Chen'."""
from mcpbrain import draft


def test_generate_draft_prompt_uses_owner(monkeypatch):
    captured = {}
    monkeypatch.setattr(draft, "_call_llm",
                        lambda prompt, model=None: captured.setdefault("p", prompt) or "draft")
    draft.generate_draft(
        "Subject", "Body", "from@x.org",
        {"key_points": []}, "voice", "", owner_full_name="Sam Jones",
    )
    assert "Sam Jones" in captured["p"]
    assert "Sam Chen" not in captured["p"]


def test_generate_draft_owner_fallback(monkeypatch):
    captured = {}
    monkeypatch.setattr(draft, "_call_llm",
                        lambda prompt, model=None: captured.setdefault("p", prompt) or "draft")
    draft.generate_draft("S", "B", "f@x", {"key_points": []}, "", "")
    assert "the account owner" in captured["p"]
