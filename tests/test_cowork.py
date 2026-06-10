"""Tests for cowork cadence prompts and the headless-claude runner."""
from pathlib import Path

import mcpbrain


def _cowork_dir():
    return Path(mcpbrain.__file__).parent / "cowork"


def test_prompts_are_shipped():
    for name in ("memory-gardener.md", "meeting-packs.md"):
        assert (_cowork_dir() / name).exists()


def test_prompts_are_generic():
    for name in ("memory-gardener.md", "meeting-packs.md"):
        text = (_cowork_dir() / name).read_text().lower()
        assert "joshbrain" not in text
        assert "centrepoint" not in text
        assert "josh" not in text
