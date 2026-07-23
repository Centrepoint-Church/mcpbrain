# tests/test_enrich_routine.py
from pathlib import Path

_ROUTINE = Path(__file__).parent.parent / "mcpbrain" / "routines" / "enrich.md"


def test_routine_is_queue_driven_and_capless():
    text = _ROUTINE.read_text()
    low = text.lower()
    # Terminator is the empty queue, not a wave/budget cap.
    assert "brain_enrich_units" in text
    assert "empty" in low
    assert "15 wave" not in low and "10 wave" not in low
    assert "budget" not in low
    # No reply string-match contract.
    assert "unit <unit_id>:" not in text
    assert "requeue guard" not in low
    # Still fans out one Haiku subagent per unit and nudges the daemon.
    assert "enrich-batch" in text
    assert "haiku" in low
    assert "brain_enrich_advance" in text
