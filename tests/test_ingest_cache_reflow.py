"""Task 13: carry-over on shared-drive cache import.

When an install imports a cache artifact for a file whose content it already
holds (every local chunk's Drive `modified` equals the artifact's), the import
goes through the reflow carry-over (reflow.plan + Store.apply_reflow) instead
of a plain replace, so this install's enrichment state and provenance
(relations, observations, actions, ...) survive the re-chunk. Spec
2026-09-24 extraction-fidelity §3/§4 ("Shared-drive ingest cache").
"""
import json

import pytest

from mcpbrain import ingest_cache
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import ENRICH_LOGIC_VERSION, ReflowOrphanError, Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")
PDF = "application/pdf"
M = "2026-09-01T10:00:00.000Z"
V = [0.1, 0.2, 0.3, 0.4]


def _store(tmp_path, name):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def _local(tmp_path, texts=("alpha beta gamma", "delta epsilon"), modified=M):
    """Install A: file F already held locally, enriched, relation on chunk 1."""
    a = _store(tmp_path, "A.sqlite3")
    for i, t in enumerate(texts):
        a.import_cached_chunk(
            f"gdrive-F-{i}", t, f"h{i}",
            {"source_type": "gdrive", "file_id": "F", "chunk_index": i,
             "chunk_total": len(texts), "drive_id": "D1", "mime_type": PDF,
             "modified": modified},
            V, enriched=True, enriched_version=ENRICH_LOGIC_VERSION)
    with a._connect(write=True) as db:
        db.execute("INSERT INTO entities(id, name, type) VALUES('e1','Dana Okafor','person')")
        db.execute("INSERT INTO entities(id, name, type) VALUES('e2','Northgate Trust','org')")
        db.execute("INSERT INTO entity_relations(entity_a, relation, entity_b, source_doc_id)"
                   " VALUES('e1','works_at','e2','gdrive-F-1')")
    return a


def _publish(tmp_path, fs, texts, modified=M, enriched=True):
    """A peer install P publishes F re-chunked under the new extractor."""
    p = _store(tmp_path, "P.sqlite3")
    for i, t in enumerate(texts):
        p.import_cached_chunk(
            f"gdrive-F-{i}", t, f"n{i}",
            {"source_type": "gdrive", "file_id": "F", "chunk_index": i,
             "chunk_total": len(texts), "drive_id": "D1", "mime_type": PDF,
             "modified": modified, "extraction_version": 1},
            V)
    if enriched:
        extraction = {
            "thread_id": "gdrive-F", "org": "unknown", "content_type": "update",
            "summary": "Marcus Reyes reviewed the budget.",
            "messages": [{"message_id": "m1", "sender": "marcus@example.org",
                          "date": "2026-09-01", "subject": "Budget"}],
            "entities": [{"name": "Marcus Reyes", "type": "person"}],
            "relations": [], "actions": [], "topics": [],
        }
        p.set_enrich_payload("F", json.dumps(extraction), ENRICH_LOGIC_VERSION)
    assert ingest_cache.publish_file(p, fs, "D1", "F", "vh2", PIN) is True


def _chunks(s):
    with s._connect() as db:
        return [tuple(r) for r in db.execute(
            "SELECT doc_id, text, enriched, enriched_version FROM chunks "
            "WHERE doc_id LIKE 'gdrive-F-%' ORDER BY doc_id")]


def _relation_doc(s):
    with s._connect() as db:
        return db.execute("SELECT source_doc_id FROM entity_relations "
                          "WHERE entity_a='e1'").fetchone()[0]


def _reflow_map(s):
    with s._connect() as db:
        return [tuple(r) for r in db.execute(
            "SELECT owner, old_doc_id, new_doc_id FROM reflow_map ORDER BY old_doc_id")]


def test_same_modified_import_carries_enrichment_and_provenance(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True

    assert _chunks(a) == [("gdrive-F-0", "alpha beta gamma\ndelta epsilon", 1,
                           ENRICH_LOGIC_VERSION)]
    assert _relation_doc(a) == "gdrive-F-0"
    assert _reflow_map(a) == [("F", "gdrive-F-0", "gdrive-F-0"),
                              ("F", "gdrive-F-1", "gdrive-F-0")]
    with a._connect() as db:
        meta = json.loads(db.execute(
            "SELECT metadata FROM chunks WHERE doc_id='gdrive-F-0'").fetchone()[0])
        nvec = db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0]
        src = db.execute("SELECT source FROM reflow_owners WHERE owner='F'").fetchone()[0]
        ent = db.execute("SELECT 1 FROM entities WHERE name='Marcus Reyes'").fetchone()
    assert meta["extraction_version"] == 1 and meta["drive_id"] == "D1"
    assert nvec == 1
    assert src == "drive_import"
    # The artifact's validated cached extraction is still applied (A#4).
    assert ent is not None


def test_uncovered_row_stays_unenriched_even_when_artifact_is_enriched(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    # Chunk 1 is text the local copy never had (e.g. recovered speaker notes).
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon",
                            "zeta speaker notes never extracted"])

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True

    assert _chunks(a) == [
        ("gdrive-F-0", "alpha beta gamma\ndelta epsilon", 1, ENRICH_LOGIC_VERSION),
        ("gdrive-F-1", "zeta speaker notes never extracted", 0, 0),
    ]
    # The relation's text now lives in chunk 0; it is remapped there, not left
    # pointing at the positional id that now holds different text.
    assert _relation_doc(a) == "gdrive-F-0"


def test_unenriched_artifact_keeps_the_plans_carried_state(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"], enriched=False)

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True

    # Local enrichment carried over by the plan even though the artifact has none.
    assert _chunks(a) == [("gdrive-F-0", "alpha beta gamma\ndelta epsilon", 1,
                           ENRICH_LOGIC_VERSION)]
    assert _relation_doc(a) == "gdrive-F-0"


def test_different_modified_takes_the_plain_replace_path(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"],
             modified="2026-09-02T09:00:00.000Z")

    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True

    # Existing behaviour: rows replaced, shrunk tail swept, nothing remapped.
    assert [c[0] for c in _chunks(a)] == ["gdrive-F-0"]
    assert _relation_doc(a) == "gdrive-F-1"
    assert _reflow_map(a) == []


def test_no_local_chunks_takes_the_plain_path(tmp_path):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])
    b = _store(tmp_path, "B.sqlite3")

    assert ingest_cache.try_import(b, fs, "D1", "F", "vh2", PIN, mime=PDF) is True
    assert [c[0] for c in _chunks(b)] == ["gdrive-F-0"]
    assert _reflow_map(b) == []


def test_lineage_gone_plan_falls_back_to_plain_replace(tmp_path, monkeypatch):
    from mcpbrain import reflow
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])

    real = reflow.plan

    def gone(old, new):
        p = real(old, new)
        p.reasons = {k: "lineage_gone" for k in p.reasons}
        return p

    monkeypatch.setattr(reflow, "plan", gone)
    assert ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF) is True
    assert [c[0] for c in _chunks(a)] == ["gdrive-F-0"]
    assert _relation_doc(a) == "gdrive-F-1"
    assert _reflow_map(a) == []


def test_orphan_error_is_raised_not_silently_replaced(tmp_path, monkeypatch):
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    a = _local(tmp_path)
    _publish(tmp_path, fs, ["alpha beta gamma\ndelta epsilon"])

    def boom(*_a, **_k):
        raise ReflowOrphanError("reflow F: 1 dangling reference(s)")

    monkeypatch.setattr(a, "apply_reflow", boom)
    with pytest.raises(ReflowOrphanError):
        ingest_cache.try_import(a, fs, "D1", "F", "vh2", PIN, mime=PDF)
    # Nothing replaced: the old chunks and the relation are untouched.
    assert [c[0] for c in _chunks(a)] == ["gdrive-F-0", "gdrive-F-1"]
    assert _relation_doc(a) == "gdrive-F-1"
