# tests/test_gardener_routine.py
from pathlib import Path

_ROUTINE = Path(__file__).parent.parent / "mcpbrain" / "routines" / "gardener.md"


def test_gardener_works_the_promotion_queue():
    """The gardener must ACT on memory_promotion findings, not just read them:
    the finding type was write-only until this step existed."""
    text = _ROUTINE.read_text()
    assert "memory_promotion" in text
    assert 'brain_proactive(finding_type="memory_promotion")' in text
    assert "brain_read" in text


def test_gardener_closes_every_promotion_finding():
    """All three outcomes resolve the finding. If any path left it open the
    queue would silently refill, which is the bug being fixed."""
    text = _ROUTINE.read_text()
    assert "brain_finding_resolve" in text
    for outcome in ("promoted", "merged", "dismissed"):
        assert outcome in text, f"outcome {outcome} not documented"


def test_gardener_promotes_through_the_write_tool():
    """Promotion must go through brain_memory_write -> daemon, documented
    specifically in the Promotion queue section (not merely present
    elsewhere in the routine, e.g. the pre-existing 'What you can update'
    section)."""
    text = _ROUTINE.read_text()
    start = text.index("## Promotion queue")
    end = text.index("\n## ", start + 1)
    section = text[start:end]
    assert "brain_memory_write(slug=" in section
    assert 'description="' in section
    assert 'body="' in section
    assert 'memory_type="project|system|preference")' in section
    assert "Do not also create the file yourself." in section
