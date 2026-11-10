"""personal_data.find names the personal identifiers a durable memory must not hold.

Consolidation used to graduate email digests into memory/*.md wholesale, so
applicants' dates of birth, personal mailboxes and phone numbers, and a
passenger's booking reference, became standing memory. The same detector
gates graduation and drives the weekly records-health warning.
"""
from mcpbrain import personal_data


def test_clean_text_finds_nothing():
    assert personal_data.find(
        "Acme runs its operations meeting fortnightly on Tuesdays at 13:00.") == []


def test_personal_mailbox_is_found_but_a_work_address_is_not():
    assert personal_data.find("Dana Okafor (dana.okafor@gmail.com) applied.") == [
        "a personal email address"]
    assert personal_data.find("Write to Sam at SAM.CHEN@Outlook.com.au today.") == [
        "a personal email address"]
    assert personal_data.find("Check-in with Pat Nguyen (pat@example.org).") == []


def test_phone_numbers_in_common_australian_forms():
    for text in ("Call 0412 345 678", "mobile 0412345678", "+61 412 345 678",
                 "phone 61412345678", "office (08) 9123 4567", "08 9123 4567"):
        assert personal_data.find(text) == ["a phone number"], text


def test_numbers_that_are_not_phones():
    for text in ("ABN 28 647 123 456", "on 2026-10-08", "1,706 rows",
                 "budget line 4100 to 4199", "at 17:00-22:00"):
        assert personal_data.find(text) == [], text


def test_dates_of_birth():
    for text in ("Dana Okafor, born 30 May 2006, applied", "born in 2012",
                 "born 2007-12-29", "Date of birth: 6 Mar 2006", "DOB 01/02/2005"):
        assert personal_data.find(text) == ["a date of birth"], text
    assert personal_data.find("a born leader and born again believer") == []


def test_travel_booking_references():
    for text in ("Booking reference 5AB7CD for Sam", "booking 5AB7CD sets check-in",
                 "e-ticket itinerary", "ticket number 0812345678901",
                 "passport number on file"):
        assert "a travel booking reference" in personal_data.find(text), text
    assert personal_data.find("booking the hall for Sunday") == []


def test_several_kinds_are_listed_once_each_in_a_fixed_order():
    text = ("Dana Okafor (dana@gmail.com, 0412 345 678, born 3 May 2006) "
            "and dana2@hotmail.com, born 2007")
    assert personal_data.find(text) == [
        "a personal email address", "a phone number", "a date of birth"]
