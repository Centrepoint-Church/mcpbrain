"""Curated timezone options carry a GMT-offset label and cover every UTC offset."""
import re
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, available_timezones

from mcpbrain import timezones

NOW = datetime(2026, 1, 15, 12, 0, tzinfo=timezone.utc)  # fixed: deterministic offsets


def test_all_zones_are_valid_iana():
    avail = available_timezones()
    for z in timezones.CURATED_ZONES:
        assert z in avail, f"{z} is not a valid IANA zone"


def test_label_format():
    label = timezones.offset_label("Australia/Perth", now=NOW)
    assert re.match(r"^Australia/Perth \(GMT[+-]\d\d:\d\d\)$", label), label


def test_zone_options_shape_and_sorted():
    opts = timezones.zone_options(now=NOW)
    assert opts and all(set(o) == {"value", "label"} for o in opts)
    # sorted by offset then name
    offsets = [ZoneInfo(o["value"]).utcoffset(NOW) for o in opts]
    assert offsets == sorted(offsets)
    # Tie-break: same-offset zones appear alphabetically
    values = [o["value"] for o in opts]
    assert values.index("Asia/Singapore") < values.index("Australia/Perth")


def test_every_offset_minus12_to_plus14_present():
    opts = timezones.zone_options(now=NOW)
    have = {int(ZoneInfo(o["value"]).utcoffset(NOW).total_seconds() // 3600) for o in opts}
    for hour in range(-12, 15):  # -12 .. +14 inclusive
        assert hour in have, f"no curated zone at GMT{hour:+d}"


def test_fractional_offset_zones_present():
    # Inhabited half-hour/45-min zones must be selectable, else those users are
    # forced to pick a wrong whole-hour zone and ClickUp deadlines shift.
    values = {z for z in timezones.CURATED_ZONES}
    for z in ("Asia/Kolkata", "Asia/Kathmandu", "Australia/Adelaide",
              "America/St_Johns", "Asia/Tehran", "Asia/Yangon",
              "Pacific/Chatham", "Pacific/Marquesas"):
        assert z in values, f"missing fractional zone {z}"


def test_fractional_offset_labels():
    # India is a stable +05:30 year-round; Nepal a stable +05:45.
    assert timezones.offset_label("Asia/Kolkata", now=NOW).endswith("(GMT+05:30)")
    assert timezones.offset_label("Asia/Kathmandu", now=NOW).endswith("(GMT+05:45)")


def test_no_duplicate_zones():
    assert len(timezones.CURATED_ZONES) == len(set(timezones.CURATED_ZONES))
