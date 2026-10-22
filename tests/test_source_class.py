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


def att(**kw):
    return gm(content_type="email_attachment", **kw)


def test_attachment_inherits_parent_class():
    assert classify("g", att(labels="DRAFT"), R) is C.DRAFT
    assert classify("g", att(subject="Daily Brief", sender=ME, to=ME), R) is C.BRIEF
    assert classify("g", att(), R) is C.PRIMARY_DOC


def test_cc_to_colleague_is_not_brief():
    m = gm(subject="Daily Brief", sender=ME, to=ME, cc="b@x.org")
    assert classify("g", m, R) is C.PRIMARY_MAIL
    assert classify("g", {**m, "cc": ""}, R) is C.BRIEF


def test_case_and_empty_to():
    m = gm(subject="Daily Brief", sender="ME@Example.ORG", to="Me <ME@EXAMPLE.org>")
    assert classify("g", m, R) is C.BRIEF
    assert classify("g", {**m, "to": ""}, R) is C.PRIMARY_MAIL


def test_session_only_for_notes():
    assert classify("g", gm(subject="Session abcdef12 2026-10-01", claude_session_id="s"), R) is C.PRIMARY_MAIL
    assert classify("a", {"source_type": "anarlog", "title": "Session abcdef12 2026-10-01",
                          "event_id": "e"}, R) is C.PRIMARY_DOC
    assert classify("note-a", {"tags": ["session"]}, R) is C.SESSION


def test_join_label_ids_keeps_system_first():
    from mcpbrain.sync.normalise import join_label_ids
    out = join_label_ids([f"Label_{i}" * 3 for i in range(40)] + ["DRAFT", "INBOX"])
    assert out.startswith("DRAFT,INBOX") and len(out) <= 200
