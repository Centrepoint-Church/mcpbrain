"""Personal identifiers that must not become durable memory.

Memory consolidation graduates high-salience email clusters into the records
repo's memory/*.md, a standing record every Claude session can read. Mail about
people (job and course applications, volunteer forms, travel bookings) carries
details that belong in the source email, not in standing memory: a personal
mailbox, a phone number, a date of birth, a booking or ticket reference.

find() names which of those a text holds. consolidation._graduate_note refuses
to graduate a note that holds any, and records-health warns on memory files
that already do. Work addresses (not a personal mailbox provider) pass, so a
note naming a colleague's work email still graduates.
"""
from __future__ import annotations

import re

# Mailbox providers people use personally. A work address on an organisation's
# own domain is not a personal detail in this sense.
PERSONAL_MAIL_DOMAINS = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "outlook.com.au", "hotmail.com", "hotmail.com.au",
    "live.com", "live.com.au", "msn.com",
    "yahoo.com", "yahoo.com.au", "ymail.com",
    "icloud.com", "me.com", "mac.com",
    "bigpond.com", "bigpond.net.au", "optusnet.com.au", "iinet.net.au",
    "tpg.com.au", "internode.on.net",
    "protonmail.com", "proton.me", "aol.com", "gmx.com",
})

_EMAIL = re.compile(r"[\w.+-]+@([\w-]+(?:\.[\w-]+)+)")

# Australian mobiles and landlines, local or +61: 0412 345 678, 0412345678,
# +61 412 345 678, 61412345678, (08) 9123 4567, 08 9123 4567. Digits on either
# side rule out longer numbers (ABNs, ticket numbers) containing a match.
_PHONE = re.compile(
    r"(?<![\d])(?:"
    r"(?:\+?61[\s-]?|0)[2-478](?:[\s-]?\d){8}"
    r"|\(0[2-478]\)[\s-]?\d{4}[\s-]?\d{4}"
    r")(?![\d])")

_MONTH = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?"
_DOB = re.compile(
    r"\bdate of birth\b|\bD\.?O\.?B\b"
    r"|\bborn\s+(?:on\s+|in\s+)?(?:\d{1,4}\b|" + _MONTH + r"\s)",
    re.IGNORECASE)

_TRAVEL = re.compile(
    r"\b(?:booking\s+ref(?:erence)?|e-?ticket|ticket\s+(?:number|no\b)"
    r"|passport\s+(?:number|no\b)|frequent\s+flyer)", re.IGNORECASE)
# "booking 5AB7CD": a six-character airline-style code with letters and digits.
_BOOKING_CODE = re.compile(
    r"\b[Bb]ooking\s+(?=[A-Z0-9]{6}\b)(?=[A-Z0-9]*\d)(?=[A-Z0-9]*[A-Z])[A-Z0-9]{6}\b")


def _personal_email(text: str) -> bool:
    return any(m.group(1).lower() in PERSONAL_MAIL_DOMAINS for m in _EMAIL.finditer(text))


def find(text: str) -> list[str]:
    """The kinds of personal identifier `text` holds, worded, each once, in a
    fixed order. Empty when it holds none."""
    found: list[str] = []
    if _personal_email(text):
        found.append("a personal email address")
    if _PHONE.search(text):
        found.append("a phone number")
    if _DOB.search(text):
        found.append("a date of birth")
    if _TRAVEL.search(text) or _BOOKING_CODE.search(text):
        found.append("a travel booking reference")
    return found
