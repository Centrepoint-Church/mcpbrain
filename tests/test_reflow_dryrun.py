"""bin/reflow_dryrun.py: the attended reflow dry run on a store COPY (plan
Task 16). Real Store copies in tmp_path; Google services and the embedder are
fakes injected through the script's two seams."""
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

from mcpbrain.org_contracts import DRIVE_ID_META_KEY
from mcpbrain.store import Store

_spec = importlib.util.spec_from_file_location(
    "reflow_dryrun", Path(__file__).resolve().parent.parent / "bin" / "reflow_dryrun.py")
dryrun = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(dryrun)

PDF = "application/pdf"
M = "2026-01-01T00:00:00Z"


class _Emb:
    dim = 4

    def __init__(self, on_embed=None):
        self.on_embed = on_embed

    def embed_passages(self, xs):
        if self.on_embed:
            self.on_embed()
        return [[0.1, 0.2, 0.3, 0.4] for _ in xs]


class _DriveSvc:
    def files(self):
        return self

    def get(self, **kw):
        class R:
            def execute(self, num_retries=0):
                return {"id": kw["fileId"], "name": "r.pdf", "mimeType": PDF,
                        "modifiedTime": M, "parents": []}
        return R()


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("MCPBRAIN_HOME", str(h))
    return h


def _copy(tmp_path, name="copy.sqlite3"):
    """A store copy holding one Shared Drive PDF owner, enriched, with a
    relation on chunk 1 -- a reflow candidate (no extraction_version)."""
    s = Store(tmp_path / name, dim=4)
    s.init()
    for i, t in enumerate(("Budget Line one", "Line two")):
        s.upsert_chunk(f"gdrive-F-{i}", t, f"h{i}",
                       {"source_type": "gdrive", "file_id": "F", "mime_type": PDF,
                        "modified": M, "chunk_index": i, "chunk_total": 2,
                        DRIVE_ID_META_KEY: "D1"})
    with s._connect(write=True) as db:
        db.execute("UPDATE chunks SET enriched=1")
        db.execute("INSERT INTO entities(id, name, type) VALUES('e1','Dana Okafor','person')")
        db.execute("INSERT INTO entities(id, name, type) VALUES('e2','Northgate Trust','org')")
        db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, source_doc_id)"
                   " VALUES('e1','works_at','e2','gdrive-F-1')")
    return tmp_path / name


def _fakes(monkeypatch, emb=None):
    from mcpbrain.sync import drive
    from mcpbrain.sync.blocks import Heading, Paragraph
    monkeypatch.setattr(dryrun, "_build_services", lambda: {"drive_service": _DriveSvc()})
    monkeypatch.setattr(dryrun, "_get_embedder", lambda: emb or _Emb())
    monkeypatch.setattr(drive, "fetch_content", lambda svc, fm, **k: drive.Content(
        text="Budget\n\nLine one\nLine two",
        blocks=[Heading(1, "Budget"), Paragraph("Line one\nLine two")]))
    monkeypatch.setattr(drive, "folder_path", lambda *a, **k: "")


def test_refuses_the_live_store_by_path_and_by_link(tmp_path, home, monkeypatch):
    from mcpbrain import config
    live = config.store_path()
    Store(live, dim=4).init()
    _fakes(monkeypatch)
    before = live.read_bytes()
    assert dryrun.main(["--store", str(live), "--yes"]) == 2
    link = tmp_path / "alias.sqlite3"
    link.symlink_to(live)
    assert dryrun.main(["--store", str(link), "--yes"]) == 2
    assert live.read_bytes() == before


def test_plan_only_without_yes_changes_nothing(tmp_path, home, monkeypatch):
    path = _copy(tmp_path)
    before = path.read_bytes()
    _fakes(monkeypatch)
    assert dryrun.main(["--store", str(path)]) == 0
    assert path.read_bytes() == before


def test_run_carries_over_never_records_a_publish_and_writes_the_summary(
        tmp_path, home, monkeypatch):
    path = _copy(tmp_path)
    out = tmp_path / "summary.json"
    _fakes(monkeypatch)
    assert dryrun.main(["--store", str(path), "--limit", "5", "--per-mime",
                        "--out", str(out), "--yes"]) == 0
    summary = json.loads(out.read_text())
    for key in ("by_outcome", "by_class", "chunks_carried", "chunks_reenrich",
                "extract_s", "embed_s", "apply_reflow_s", "gmail_calendar_source_changed",
                "orphans_before", "orphans_after", "orphans_new", "foreign_key_check",
                "integrity_check", "problems"):
        assert key in summary
    assert summary["by_outcome"] == {"carried": 1}
    assert summary["by_class"] == {"drive:pdf": {"carried": 1}}
    assert summary["chunks_carried"] >= 1
    assert summary["apply_reflow_s"]["n"] == 1 and summary["extract_s"]["n"] == 1
    assert summary["integrity_check"] == "ok" and summary["orphans_new"] == {}
    # Never publishes: nothing is queued for the fleet on the copy.
    assert summary["pending_publishes_recorded"] == 0
    assert Store(path, dim=4).pending_publishes("D1") == []


def test_an_injected_orphan_exits_non_zero(tmp_path, home, monkeypatch):
    path = _copy(tmp_path)

    def dangle():
        with sqlite3.connect(path) as db:
            db.execute("INSERT INTO recall_feedback(doc_id, event_type) "
                       "VALUES('gdrive-GONE-9','exposure')")

    _fakes(monkeypatch, emb=_Emb(on_embed=dangle))
    out = tmp_path / "summary.json"
    assert dryrun.main(["--store", str(path), "--out", str(out), "--yes"]) == 1
    summary = json.loads(out.read_text())
    assert summary["orphans_new"] == {"recall_feedback.doc_id": 1}
    assert any("orphan" in p for p in summary["problems"])


def test_pre_existing_orphans_only_fail_under_strict(tmp_path, home, monkeypatch):
    path = _copy(tmp_path)
    with sqlite3.connect(path) as db:
        db.execute("INSERT INTO recall_feedback(doc_id, event_type) "
                   "VALUES('gdrive-LONG-GONE-0','exposure')")
    _fakes(monkeypatch)
    assert dryrun.main(["--store", str(path), "--yes"]) == 0
    path2 = _copy(tmp_path, "copy2.sqlite3")
    with sqlite3.connect(path2) as db:
        db.execute("INSERT INTO recall_feedback(doc_id, event_type) "
                   "VALUES('gdrive-LONG-GONE-0','exposure')")
    assert dryrun.main(["--store", str(path2), "--strict-orphans", "--yes"]) == 1
