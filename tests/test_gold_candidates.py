import importlib.util
import pathlib

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
