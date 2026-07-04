"""A#4 Task 5: end-to-end round-trip + revocation GC + echo-safety.

Verifies the merged capture -> store -> publish -> apply pipeline (Tasks 1-4)
actually delivers the value proposition: a peer install can import a cached
enrichment payload and get graph rows written WITHOUT re-running extraction,
a revoked drive's cached payloads are garbage-collected, and repeated
source_ref derivation for the same doc is stable (curator-side dedup safety).
"""
import json

from mcpbrain.store import Store


def _store(tmp_path, name="a.sqlite3"):
    s = Store(tmp_path / name, dim=4)
    s.init()
    return s


def test_enrichment_payload_round_trips_publisher_to_importer(tmp_path):
    """Store A imports+enriches a drive doc and publishes it; store B imports
    from the shared fleet storage and gets the SAME graph rows applied without
    re-running Haiku (enriched flag carries, and the entity lands in B's
    entities table straight from the cached payload)."""
    from mcpbrain import ingest_cache
    from mcpbrain.org_contracts import FleetPin
    from tests.helpers.org_fleet import LocalDirFleetStorage

    pin = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
                   enrich_logic_floor=1, fleet_secret="s3cret")
    fs = LocalDirFleetStorage(tmp_path / "fleet")

    A = _store(tmp_path, "A.sqlite3")
    doc_id = "gdrive-F1-0"
    A.import_cached_chunk(
        doc_id, "Ada Lovelace is leading the analytics project.", "ch0",
        {"source_type": "gdrive", "file_id": "F1", "chunk_index": 0, "drive_id": "D1"},
        [0.1, 0.2, 0.3, 0.4])

    extraction = {
        "thread_id": "gdrive-F1", "org": "unknown", "content_type": "update",
        "summary": "Ada Lovelace is leading the analytics project.",
        "messages": [{"message_id": "m1", "sender": "ada@example.org",
                      "date": "2026-01-01", "subject": "Analytics project"}],
        "entities": [{"name": "Ada Lovelace", "type": "person"}],
        "relations": [], "actions": [], "topics": [],
    }
    A.set_enrich_payload(doc_id, json.dumps(extraction), 1)

    assert ingest_cache.publish_file(A, fs, "D1", "F1", "vh1", pin) is True

    B = _store(tmp_path, "B.sqlite3")
    assert ingest_cache.try_import(B, fs, "D1", "F1", "vh1", pin) is True

    with B._connect() as db:
        row = db.execute(
            "SELECT enriched FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
        assert row is not None
        assert row["enriched"] == 1

        ent = db.execute(
            "SELECT name FROM entities WHERE name=?", ("Ada Lovelace",)).fetchone()
    assert ent is not None, "cached enrichment payload was not applied on import"


def test_purge_drive_drops_enrich_payloads(tmp_path):
    from mcpbrain import ingest_cache
    s = _store(tmp_path)
    s.import_cached_chunk("gdrive-F1-0", "t", "h",
                          {"source_type": "gdrive", "file_id": "F1", "drive_id": "D1"}, [0.0] * 4)
    s.set_enrich_payload("gdrive-F1-0", '{"thread_id":"gdrive-F1"}', 1)
    assert s.get_enrich_payload("gdrive-F1-0") is not None
    ingest_cache.purge_drive(s, "D1")
    assert s.get_enrich_payload("gdrive-F1-0") is None


def test_cached_enrichment_echo_is_corroboration_safe(tmp_path):
    """A#4-scoped confirmation: two importers deriving source_ref for the same
    cached doc get the SAME value, so the curator's corroboration counting
    sees one source rather than double-counting an echo (deep version already
    covered by Phase D Task 1)."""
    from mcpbrain.org_contracts import source_ref
    a = source_ref("s3cret", "gdrive-FID-0")
    b = source_ref("s3cret", "gdrive-FID-0")
    assert a == b
