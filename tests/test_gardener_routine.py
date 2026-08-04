# tests/test_gardener_routine.py
from pathlib import Path

from mcpbrain import tools as _tools

_ROUTINE = Path(__file__).parent.parent / "mcpbrain" / "routines" / "gardener.md"


def _promotion_section(text):
    """Slice out just the '## Promotion queue' section (up to the next '## '
    heading) so tests check that section specifically, not a stray mention
    anywhere in the file."""
    start = text.index("## Promotion queue")
    end = text.index("\n## ", start + 1)
    return text[start:end]


def test_gardener_works_the_promotion_queue():
    """The gardener must ACT on memory_promotion findings, not just read them:
    the finding type was write-only until this step existed."""
    section = _promotion_section(_ROUTINE.read_text())
    for finding_type in _tools.MANUAL_RESOLVE_TYPES:
        assert finding_type in section
    assert (
        f'brain_proactive(finding_type="{_tools.MANUAL_RESOLVE_TYPES[0]}")'
        in section
    )
    assert "brain_read" in section


def test_gardener_closes_every_promotion_finding():
    """All three outcomes resolve the finding. If any path left it open the
    queue would silently refill, which is the bug being fixed."""
    section = _promotion_section(_ROUTINE.read_text())
    assert "brain_finding_resolve" in section
    for outcome in _tools._RESOLVE_OUTCOMES:
        assert outcome in section, f"outcome {outcome} not documented"


def test_gardener_promotes_through_the_write_tool():
    """Promotion must go through brain_memory_write -> daemon, documented
    specifically in the Promotion queue section (not merely present
    elsewhere in the routine, e.g. the pre-existing 'What you can update'
    section)."""
    section = _promotion_section(_ROUTINE.read_text())
    assert "brain_memory_write(slug=" in section
    assert 'description="' in section
    assert 'body="' in section
    assert 'memory_type="project|system|preference")' in section
    assert "Do not also create the file yourself." in section
