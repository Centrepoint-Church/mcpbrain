from mcpbrain import config
from mcpbrain.source_class import (
    EVIDENCE, Rules, SourceClass as C, classify, is_evidence,
)

ME = "me@example.org"
R = Rules(frozenset({ME}), ("Daily Brief",), ("My Drive/Daily Briefs",), frozenset({"Label_gen"}))


def gm(**kw):
    m = {"source_type": "gmail", "subject": "Hi", "sender": "A <a@x.org>", "to": ME, "labels": "INBOX"}
    m.update(kw)
    return m


def test_digest_duplicate_consolidated():
    assert classify("enriched-abc", {}, R) is C.DIGEST
    assert classify("gmail-1", gm(dup_of="gmail-0"), R) is C.DUPLICATE
    assert classify("note-consolidated-ab", {}, R) is C.CONSOLIDATED
    assert classify("note-x", {"observation_type": "consolidated"}, R) is C.CONSOLIDATED


def test_session_by_tag_title_or_id():
    assert classify("note-a", {"tags": "session,pre-compact"}, R) is C.SESSION
    assert classify("note-a", {"title": "Session abcdef12 2026-10-01"}, R) is C.SESSION
    assert classify("note-a", {"claude_session_id": "s1"}, R) is C.SESSION
    assert classify("note-a", {"tags": "ideas", "title": "Plan"}, R) is C.PRIMARY_DOC


def test_anarlog_session_id_is_not_claude_session():
    assert classify("anarlog-1", {"source_type": "anarlog", "session_id": "z"}, R) is C.PRIMARY_DOC


def test_gmail_draft_primary_and_marker():
    assert classify("g", gm(labels="DRAFT"), R) is C.DRAFT
    assert classify("g", gm(), R) is C.PRIMARY_MAIL
    assert classify("g", gm(labels="INBOX,Label_gen"), R) is C.BRIEF


def test_brief_rule_requires_self_sent():
    m = gm(subject="Daily Brief 1", sender=f"Me <{ME}>", to=ME)
    assert classify("g", m, R) is C.BRIEF
    assert classify("g", gm(subject="Daily Brief 1"), R) is C.PRIMARY_MAIL
    assert classify("g", gm(subject="Daily Brief 1", sender=ME, to=f"{ME}, b@x.org"), R) is C.PRIMARY_MAIL
    assert classify("g", gm(subject="Daily Brief 1", sender=ME, to=ME), Rules(frozenset({ME}))) is C.PRIMARY_MAIL


def test_folder_rule_matches_subfolders_not_prefix_names():
    d = {"source_type": "gdrive", "folder_path": "My Drive/Daily Briefs/x"}
    assert classify("gdrive-1", d, R) is C.BRIEF
    assert classify("gdrive-1", {**d, "folder_path": "My Drive/Daily Briefs"}, R) is C.BRIEF
    assert classify("gdrive-1", {**d, "folder_path": "My Drive/Daily Briefs Archive"}, R) is C.PRIMARY_DOC
    assert classify("gdrive-1", {"source_type": "gdrive", "app_generated": "1"}, R) is C.BRIEF


def test_calendar_attachment_other():
    assert classify("cal-1-0", {"source_type": "calendar", "event_id": "1"}, R) is C.PRIMARY_EVENT
    assert classify("g", gm(content_type="email_attachment"), R) is C.PRIMARY_DOC


def test_evidence_set():
    assert is_evidence(C.PRIMARY_MAIL) and not is_evidence(C.BRIEF)
    assert EVIDENCE == {C.PRIMARY_MAIL, C.PRIMARY_DOC, C.PRIMARY_EVENT}
    assert C.DUPLICATE.value == "duplicate"


def test_from_config(tmp_path):
    home = str(tmp_path)
    assert config.generated_gmail_subject_prefixes(home) == []
    config.write_config(home, {"owner_email": "Me@Example.org",
                               "ingest": {"generated_gmail_subject_prefixes": ["X"],
                                          "generated_drive_folders": ["F"]}})
    (tmp_path / "google_account").write_text("Alt@Example.org\n")
    r = Rules.from_config(home, generated_label_ids={"L"})
    assert "alt@example.org" in r.owner_emails
    assert r.generated_label_ids == {"L"}
    assert "me@example.org" in r.owner_emails
    assert r.subject_prefixes == ("X",) and r.drive_folders == ("F",)
