"""bin/stamp_audited_prose.py — accept audited v0 prose without losing its history.

The point of this script is a claim: these chunks are OLD, not DAMAGED, so they
need no re-fetch. That claim is only safe if the stamp is precise about what it
covers and honest about what it overwrites.
"""
import json

import pytest

from bin import stamp_audited_prose as sap
from mcpbrain.chunking import CHUNKER_VERSION
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    return s


def _chunk(s, doc_id, text, *, version=0, subtype=None):
    meta = {"source_type": "gmail"}
    if version:
        meta["chunker_version"] = version
    if subtype:
        meta["content_subtype"] = subtype
    s.upsert_chunk(doc_id, text, f"h-{doc_id}", meta)


def _meta(s, doc_id):
    with s._connect() as db:
        return json.loads(
            db.execute("SELECT metadata FROM chunks WHERE doc_id=?",
                       (doc_id,)).fetchone()["metadata"])


def _run(tmp_path, *args):
    return sap.main([*args, "--home", str(tmp_path)])


def test_stamps_audited_v0_prose_and_records_what_it_really_was(tmp_path):
    s = _store(tmp_path)
    _chunk(s, "prose-1", "ordinary prose well within the window")
    _run(tmp_path, "--yes")
    m = _meta(s, "prose-1")
    assert m["chunker_version"] == CHUNKER_VERSION
    assert m["chunker_upgraded_from"] == 0, "must record the TRUE prior version"
    assert m["chunker_audited"], "must record when the audit accepted it"


def test_the_stamped_set_is_findable_afterwards(tmp_path):
    """The whole safety argument rests on this: if the audit was wrong, or a
    future chunker bump needs to reach these, they must still be identifiable.
    A bare chunker_version stamp would make that impossible."""
    s = _store(tmp_path)
    _chunk(s, "prose-1", "ordinary prose")
    _chunk(s, "already-current", "current prose", version=CHUNKER_VERSION)
    _run(tmp_path, "--yes")
    with s._connect() as db:
        found = [r["doc_id"] for r in db.execute(
            "SELECT doc_id FROM chunks WHERE "
            "json_extract(metadata,'$.chunker_upgraded_from') IS NOT NULL")]
    assert found == ["prose-1"]


@pytest.mark.parametrize("doc_id,text,subtype", [
    ("oversize", "x" * (sap.OVERSIZE_CHARS + 1), None),   # tail has no vector
    ("table", "a,b,c", "table"),                          # superseded rendering
    ("empty", "   \n\t  ", None),                         # content-free
])
def test_leaves_genuinely_damaged_chunks_for_the_reingest_sweep(tmp_path, doc_id,
                                                                text, subtype):
    """Each of these fails one of the three v1->v2 criteria, so the audit does
    NOT cover it — stamping would hide it from reingest-stale permanently."""
    s = _store(tmp_path)
    _chunk(s, doc_id, text, subtype=subtype)
    _run(tmp_path, "--yes")
    m = _meta(s, doc_id)
    assert m.get("chunker_version", 0) == 0, f"{doc_id} must stay stale"
    assert "chunker_upgraded_from" not in m


def test_dry_run_writes_nothing(tmp_path):
    s = _store(tmp_path)
    _chunk(s, "prose-1", "ordinary prose")
    _run(tmp_path)
    assert _meta(s, "prose-1").get("chunker_version", 0) == 0


def test_is_idempotent_and_never_clobbers_the_recorded_origin(tmp_path):
    """A second run must not rewrite chunker_upgraded_from to 3 — that would
    erase the very fact the first run existed to preserve."""
    s = _store(tmp_path)
    _chunk(s, "prose-1", "ordinary prose")
    _run(tmp_path, "--yes")
    first = _meta(s, "prose-1")
    _run(tmp_path, "--yes")
    assert _meta(s, "prose-1") == first


def test_does_not_touch_chunks_already_at_the_current_version(tmp_path):
    s = _store(tmp_path)
    _chunk(s, "current", "prose", version=CHUNKER_VERSION)
    _run(tmp_path, "--yes")
    assert "chunker_upgraded_from" not in _meta(s, "current")
