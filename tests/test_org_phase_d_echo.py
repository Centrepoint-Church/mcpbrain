import base64
import struct

from mcpbrain import org_contracts as oc
from mcpbrain import org_curate, ingest_cache
from mcpbrain.org_contracts import FleetPin, CacheArtifact, CacheChunk, artifact_filename
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage


def _store(tmp_path, name="s.sqlite3"):
    s = Store(tmp_path / name, dim=4); s.init(); return s


def test_shared_doc_echo_yields_one_source_ref_across_contributors():
    # The SAME doc, hashed by N contributors sharing the fleet_secret, collapses
    # to ONE source_ref — so corroboration (counted by distinct srefs) can never
    # be inflated by many members importing the same cached enrichment.
    secret = "s3cret"
    refs = {oc.source_ref(secret, "gdrive-DOC-1") for _ in range(5)}
    assert len(refs) == 1


def test_corroboration_not_inflated_by_echo():
    # 5 contributors, one shared doc (one sref) -> NOT corroborated for the
    # strict type; a genuinely independent 2-source/2-contributor set IS.
    echo = {"srefs": {"H"}, "contribs": {f"u{i}@x.org" for i in range(5)}}
    assert org_curate._corroborated("mentioned_with", echo) is False
    indep = {"srefs": {"H1", "H2"}, "contribs": {"a@x.org", "b@x.org"}}
    assert org_curate._corroborated("mentioned_with", indep) is True


def test_cache_import_writes_no_graph_rows(tmp_path):
    # The A->B echo path is INERT: importing a cache artifact populates chunks
    # only, never entities/relations, so there is nothing for the contribution
    # edge to re-emit. (A#4 would change this and is out of scope for Phase D.)
    s = _store(tmp_path)
    fs = LocalDirFleetStorage(tmp_path / "drv")
    pin = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
                   enrich_logic_floor=1, fleet_secret="s3cret")
    vec_b64 = base64.b64encode(struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)).decode()
    art = CacheArtifact(
        file_id="FID", content_hash="vh1", extraction_method="gdocs",
        chunker_version="v1", embed_model="bge-small", dim=4,
        chunks=(CacheChunk(idx=0, text="Acme quarterly numbers", embedding_b64=vec_b64,
                           metadata={"source_type": "gdrive", "file_id": "FID", "chunk_index": 0}),),
        enrich={"logic_version": 1}, published_by="p@x.org", published_at="2026-07-04")
    import gzip, json
    fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename('FID','vh1','bge-small',4,'v1')}",
                 gzip.compress(json.dumps(art.to_dict()).encode()))
    assert ingest_cache.try_import(s, fs, "D1", "FID", "vh1", pin) is True
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entities").fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM entity_relations").fetchone()["c"] == 0
        assert db.execute("SELECT COUNT(*) c FROM chunks").fetchone()["c"] == 1
