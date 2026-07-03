"""Boulder 3: calendar bridge for meeting-series consolidation.

Covers recurringEventId capture (Task 8) and the opportunistic
calendar_series annotation (Task 9).
"""

from mcpbrain.sync.calendar import normalise_calendar


def test_recurring_event_id_captured():
    ev = {"id": "occ123", "recurringEventId": "series999", "status": "confirmed",
          "summary": "Standup", "start": {"date": "2026-05-12"}, "end": {"date": "2026-05-12"}}
    chunks = normalise_calendar(ev)
    assert chunks[0].metadata["recurring_event_id"] == "series999"


def test_non_recurring_event_id_blank():
    ev = {"id": "e1", "status": "confirmed", "summary": "One-off",
          "start": {"date": "2026-05-12"}, "end": {"date": "2026-05-12"}}
    chunks = normalise_calendar(ev)
    assert chunks[0].metadata["recurring_event_id"] == ""
