"""Boulder 3: calendar bridge for meeting-series consolidation.

Covers recurringEventId capture (Task 8) and the opportunistic
calendar_series annotation (Task 9).
"""

import pytest

from mcpbrain.graph_write import owner_identity_from_config
from mcpbrain.store import Store
from mcpbrain.sync import calendar as cal
from mcpbrain.sync.calendar import normalise_calendar


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "brain.db", dim=4)
    s.init()
    return s


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


def test_annotate_series_from_recurring_event(store):
    # Seed a meeting series the way apply() would (org 'external' — no owner org).
    store.upsert_entity("meeting-external-standup", "Standup", "meeting", "external", "2026-05-01")
    ev = {"id": "occ1", "recurringEventId": "series999", "status": "confirmed",
          "summary": "Standup", "start": {"date": "2026-05-12"}, "end": {"date": "2026-05-12"}}
    owner = owner_identity_from_config()
    assert cal._annotate_series_from_event(store, ev, owner) is True
    with store._connect() as db:
        row = db.execute(
            "SELECT value FROM entity_observations "
            "WHERE entity_id='meeting-external-standup' AND attribute='calendar_series'"
        ).fetchone()
    assert row["value"] == "series999"


def test_annotate_noop_without_matching_series(store):
    ev = {"id": "occ1", "recurringEventId": "series999", "status": "confirmed",
          "summary": "Nonexistent Meeting", "start": {"date": "2026-05-12"}}
    owner = owner_identity_from_config()
    assert cal._annotate_series_from_event(store, ev, owner) is False


def test_annotate_noop_for_non_recurring(store):
    store.upsert_entity("meeting-external-standup", "Standup", "meeting", "external", "2026-05-01")
    ev = {"id": "e1", "status": "confirmed", "summary": "Standup", "start": {"date": "2026-05-12"}}
    owner = owner_identity_from_config()
    assert cal._annotate_series_from_event(store, ev, owner) is False
