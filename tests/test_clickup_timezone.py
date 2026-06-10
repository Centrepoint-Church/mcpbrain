from mcpbrain import clickup


def test_deadline_uses_configured_tz():
    # 2026-06-10 midnight in New York (UTC-4 in June) = 04:00 UTC
    ms = clickup.deadline_to_due_ms("2026-06-10", tz="America/New_York")
    from datetime import datetime, timezone
    got = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    assert got.hour == 4 and got.day == 10


def test_deadline_none_when_tz_unset():
    assert clickup.deadline_to_due_ms("2026-06-10", tz="") is None


def test_roundtrip_non_perth():
    ms = clickup.deadline_to_due_ms("2026-06-10", tz="America/New_York")
    assert clickup.due_ms_to_deadline(ms, tz="America/New_York") == "2026-06-10"
