import importlib.util
import os
import pathlib

import pytest
import yaml

from mcpbrain.store import Store

spec = importlib.util.spec_from_file_location(
    "gold_candidates", pathlib.Path(__file__).parents[1] / "bin" / "gold_candidates.py")
gc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(gc)


def test_draft_picks_multi_chunk_documents_and_leaves_query_blank(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(4):
        s.upsert_chunk(f"gdrive-F1-{i}", f"section {i} text", f"h{i}",
                       {"file_id": "F1", "mime_type": "application/pdf"})
    s.upsert_chunk("gdrive-F2-0", "single", "hx", {"file_id": "F2", "mime_type": "application/pdf"})
    out = tmp_path / "c.yaml"
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=5)
    cases = yaml.safe_load(out.read_text())
    assert len(cases) == 1
    ids = cases[0]["expected_chunk_ids"]
    assert len(set(ids)) == 2 and all(i.startswith("gdrive-F1-") for i in ids)
    assert cases[0]["query"] == ""
    assert gc.verify(str(tmp_path / "b.sqlite3"), str(out)) == (2, 2)


def test_draft_id_is_stable_and_notes_are_short(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(3):
        s.upsert_chunk(f"gdrive-F9-{i}", "x" * 200, f"h{i}",
                       {"file_id": "F9", "mime_type": "application/pdf"})
    out = tmp_path / "c.yaml"
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=5)
    cases = yaml.safe_load(out.read_text())
    assert len(cases) == 1
    assert cases[0]["id"] == "cand_gdrive-F9"
    # a re-run must give the same candidate (deterministic selection + id)
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=5)
    again = yaml.safe_load(out.read_text())
    assert again == cases
    assert len(cases[0]["notes"]) <= 2 * 120 + 10  # never leaks more than ~120 chars/chunk


def test_draft_refuses_an_out_path_inside_this_repo(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(3):
        s.upsert_chunk(f"gdrive-F1-{i}", "text", f"h{i}", {"file_id": "F1"})
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    bad_out = repo_root / "tests" / "_should_not_exist.yaml"
    try:
        raised = False
        try:
            gc.draft(str(tmp_path / "b.sqlite3"), str(bad_out), n=5)
        except SystemExit as exc:
            raised = True
            assert exc.code == 2
        assert raised
        assert not bad_out.exists()
    finally:
        if bad_out.exists():
            bad_out.unlink()


def test_draft_allows_a_path_outside_the_repo(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(3):
        s.upsert_chunk(f"gdrive-F1-{i}", "text", f"h{i}",
                       {"file_id": "F1", "mime_type": "application/pdf"})
    out = tmp_path / "outside" / "c.yaml"
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=5)
    assert out.exists()
    assert len(yaml.safe_load(out.read_text())) == 1


def test_draft_refuses_a_case_varied_path_into_the_repo(tmp_path):
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    s_root = str(repo_root)
    idx = s_root.rfind(os.sep)
    varied_root = s_root[: idx + 1] + s_root[idx + 1 :].upper()
    if not os.path.exists(varied_root):
        pytest.skip("filesystem is case-sensitive")

    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(3):
        s.upsert_chunk(f"gdrive-F1-{i}", "text", f"h{i}", {"file_id": "F1"})
    bad_out = pathlib.Path(varied_root) / "tests" / "_should_not_exist_case.yaml"
    real_bad = repo_root / "tests" / "_should_not_exist_case.yaml"
    try:
        with pytest.raises(SystemExit) as exc_info:
            gc.draft(str(tmp_path / "b.sqlite3"), str(bad_out), n=5)
        assert exc_info.value.code == 2
        assert not real_bad.exists()
    finally:
        if real_bad.exists():
            real_bad.unlink()


def test_draft_refuses_a_symlink_into_the_repo(tmp_path):
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(3):
        s.upsert_chunk(f"gdrive-F1-{i}", "text", f"h{i}", {"file_id": "F1"})
    link = tmp_path / "repo_tests_link"
    link.symlink_to(repo_root / "tests")
    bad_out = link / "_should_not_exist_symlink.yaml"
    real_bad = repo_root / "tests" / "_should_not_exist_symlink.yaml"
    try:
        with pytest.raises(SystemExit) as exc_info:
            gc.draft(str(tmp_path / "b.sqlite3"), str(bad_out), n=5)
        assert exc_info.value.code == 2
        assert not real_bad.exists()
    finally:
        if real_bad.exists():
            real_bad.unlink()


def test_draft_never_selects_synthetic_or_excluded_document_kinds(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    # the one real, qualifying document
    for i in range(3):
        s.upsert_chunk(f"gdrive-F1-{i}", f"section {i}", f"h{i}",
                       {"file_id": "F1", "mime_type": "application/pdf"})
    # everything else: many chunks each, none of these kinds may ever be picked
    for i in range(4):
        s.upsert_chunk(f"enriched-T1-{i}", f"digest {i}", f"eh{i}", {"thread_id": "T1"})
    for i in range(4):
        s.upsert_chunk(f"anarlog-S1-notes-{i}", f"meeting {i}", f"ah{i}", {"session_id": "S1"})
    for i in range(4):
        s.upsert_chunk(f"cal-E1-{i}", f"event {i}", f"ch{i}", {"event_id": "E1"})
    for i in range(4):
        s.upsert_chunk(f"note-{'a' * 32}-{i}", f"note {i}", f"nh{i}", {})
    out = tmp_path / "c.yaml"
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=20)
    cases = yaml.safe_load(out.read_text())
    assert {c["id"] for c in cases} == {"cand_gdrive-F1"}


def test_draft_excludes_a_drive_file_with_a_non_block_mime(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(4):
        s.upsert_chunk(f"gdrive-F2-{i}", f"row {i}", f"h{i}",
                       {"file_id": "F2", "mime_type": "application/vnd.google-apps.spreadsheet"})
    out = tmp_path / "c.yaml"
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=20)
    assert yaml.safe_load(out.read_text()) == []


def test_draft_a_gmail_message_with_two_body_chunks_qualifies(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("gmail-M1-body-0", "first half", "h0", {"message_id": "M1"})
    s.upsert_chunk("gmail-M1-body-1", "second half", "h1", {"message_id": "M1"})
    # an attachment chunk on the same message must never count towards, or
    # substitute for, the body group.
    s.upsert_chunk("gmail-M1-att-0-0", "attachment text", "ha0", {"message_id": "M1"})
    out = tmp_path / "c.yaml"
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=20)
    cases = yaml.safe_load(out.read_text())
    assert len(cases) == 1
    assert cases[0]["id"] == "cand_gmail-M1-body"
    assert set(cases[0]["expected_chunk_ids"]) == {"gmail-M1-body-0", "gmail-M1-body-1"}


def test_draft_mixes_drive_and_gmail_and_is_stable(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for d in range(4):
        for i in range(3):
            s.upsert_chunk(f"gdrive-D{d}-{i}", f"doc {d} part {i}", f"hd{d}{i}",
                           {"file_id": f"D{d}", "mime_type": "application/pdf"})
    for g in range(4):
        s.upsert_chunk(f"gmail-G{g}-body-0", f"mail {g} part 0", f"hg{g}0", {"message_id": f"G{g}"})
        s.upsert_chunk(f"gmail-G{g}-body-1", f"mail {g} part 1", f"hg{g}1", {"message_id": f"G{g}"})
    out = tmp_path / "c.yaml"
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=4)
    first = yaml.safe_load(out.read_text())
    assert len(first) == 4
    drive_n = sum(1 for c in first if c["id"].startswith("cand_gdrive-"))
    gmail_n = sum(1 for c in first if c["id"].startswith("cand_gmail-"))
    assert (drive_n, gmail_n) == (2, 2)

    # deterministic: a re-run against the unchanged store is byte-identical
    gc.draft(str(tmp_path / "b.sqlite3"), str(out), n=4)
    second = yaml.safe_load(out.read_text())
    assert second == first


def test_verify_reports_missing_ids(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("gdrive-F1-0", "text", "h0", {"file_id": "F1"})
    out = tmp_path / "c.yaml"
    out.write_text(yaml.safe_dump([
        {"id": "cand_x", "query": "", "expected_chunk_ids": ["gdrive-F1-0", "gdrive-F1-99"],
         "notes": ""},
    ]))
    assert gc.verify(str(tmp_path / "b.sqlite3"), str(out)) == (1, 2)
