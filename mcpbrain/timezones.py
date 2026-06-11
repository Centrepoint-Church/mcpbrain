"""Curated IANA timezones with human GMT-offset labels for the setup dropdown.

A short, sorted list with at least one representative zone for every whole-hour
UTC offset from -12 to +14, PLUS the inhabited half-hour and 45-minute offsets
(India +5:30, Nepal +5:45, Adelaide +9:30, Newfoundland -3:30, Iran +3:30,
Myanmar +6:30, Chatham +12:45, Marquesas -9:30), so a user anywhere can pick a
correct zone — a wrong zone shifts ClickUp deadlines. Offsets are computed at a
caller-supplied `now` (DST-correct, deterministic in tests).
"""
from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

# At least one representative per inhabited UTC offset, whole-hour -12..+14 plus
# the fractional offsets. zone_options() sorts by true offset, so list order here
# is for readability only.
CURATED_ZONES: tuple[str, ...] = (
    "Etc/GMT+12",            # GMT-12 (POSIX sign inversion: Etc/GMT+N = UTC−N)
    "Pacific/Pago_Pago",     # GMT-11
    "Pacific/Honolulu",      # GMT-10
    "Pacific/Marquesas",     # GMT-09:30
    "America/Anchorage",     # GMT-09
    "America/Los_Angeles",   # GMT-08
    "America/Denver",        # GMT-07
    "America/Chicago",       # GMT-06
    "America/New_York",      # GMT-05
    "America/Halifax",       # GMT-04
    "America/St_Johns",      # GMT-03:30 (Newfoundland)
    "America/Sao_Paulo",     # GMT-03
    "Atlantic/South_Georgia",# GMT-02
    "Atlantic/Azores",       # GMT-01
    "Europe/London",         # GMT+00
    "Europe/Paris",          # GMT+01
    "Europe/Athens",         # GMT+02
    "Europe/Moscow",         # GMT+03
    "Asia/Tehran",           # GMT+03:30 (Iran)
    "Asia/Dubai",            # GMT+04
    "Asia/Karachi",          # GMT+05
    "Asia/Kolkata",          # GMT+05:30 (India)
    "Asia/Kathmandu",        # GMT+05:45 (Nepal)
    "Asia/Dhaka",            # GMT+06
    "Asia/Yangon",           # GMT+06:30 (Myanmar)
    "Asia/Bangkok",          # GMT+07
    "Asia/Singapore",        # GMT+08
    "Australia/Perth",       # GMT+08 (common; same offset, different name)
    "Asia/Tokyo",            # GMT+09
    "Australia/Darwin",      # GMT+09:30 (no DST)
    "Australia/Adelaide",    # GMT+09:30 / +10:30 DST
    "Australia/Sydney",      # GMT+10 (DST varies)
    "Australia/Brisbane",    # GMT+10 (no DST)
    "Pacific/Noumea",        # GMT+11
    "Pacific/Tarawa",        # GMT+12 (no DST; Auckland is +13 in Jan DST)
    "Pacific/Auckland",      # GMT+13 in Jan (DST); GMT+12 in winter
    "Pacific/Chatham",       # GMT+12:45 / +13:45 DST
    "Pacific/Tongatapu",     # GMT+13
    "Pacific/Kiritimati",    # GMT+14
)


def offset_label(zone: str, *, now: datetime) -> str:
    """Return '<zone> (GMT±HH:MM)' for `zone` at `now`."""
    off = ZoneInfo(zone).utcoffset(now)
    if off is None:
        return f"{zone} (GMT offset unknown)"
    total = int(off.total_seconds())
    sign = "+" if total >= 0 else "-"
    total = abs(total)
    return f"{zone} (GMT{sign}{total // 3600:02d}:{(total % 3600) // 60:02d})"


def zone_options(*, now: datetime) -> list[dict]:
    """[{'value','label'}] for the curated set, sorted by offset then name.

    A zone that fails to resolve (bad tzdata) is skipped, never fatal.
    """
    out = []
    for z in CURATED_ZONES:
        try:
            off = ZoneInfo(z).utcoffset(now)
        except Exception:  # noqa: BLE001 — skip an unresolvable zone
            continue
        out.append((off, z))
    out.sort(key=lambda t: (t[0], t[1]))
    return [{"value": z, "label": offset_label(z, now=now)} for _, z in out]
