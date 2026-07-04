import base64
import gzip
import json
import struct

from mcpbrain import onboarding, org_curate, ingest_cache
from mcpbrain.org_contracts import (FleetPin, CacheArtifact, CacheChunk,
                                    artifact_filename)
from mcpbrain.store import Store
from tests.helpers.org_fleet import make_fleet, LocalDirFleetStorage

PIN = FleetPin(embed_model="bge-small", dim=4, chunker_version="v1",
               enrich_logic_floor=1, fleet_secret="s3cret")


def _publish_cache_artifact(drive_fs, file_id="F1"):
    vec = base64.b64encode(struct.pack("<4f", 0.1, 0.2, 0.3, 0.4)).decode()
    art = CacheArtifact(
        file_id=file_id, content_hash="vh1", extraction_method="gdocs",
        chunker_version="v1", embed_model="bge-small", dim=4,
        chunks=(CacheChunk(idx=0, text="shared drive doc body", embedding_b64=vec,
                           metadata={"source_type": "gdrive", "file_id": file_id, "chunk_index": 0}),),
        enrich={}, published_by="p@x.org", published_at="2026-07-04")
    drive_fs.put_bytes(f"{ingest_cache.CACHE_DIR}/{artifact_filename(file_id,'vh1','bge-small',4,'v1')}",
                       gzip.compress(json.dumps(art.to_dict()).encode()))


def test_new_member_bootstraps_from_real_snapshot_and_cache(tmp_path):
    members, curator, fleet_fs = make_fleet(tmp_path, n_members=1)
    (bob,) = members

    # (a) curator has an org entity + relation and publishes a real snapshot.
    with curator.store._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,email_addr) "
                   "VALUES('joel','Joel Chelliah','person','org','joel@acme.org')")
        db.execute("INSERT INTO entities(id,name,type,origin) VALUES('acme','Acme','org','org')")
        db.execute("INSERT INTO entity_relations(entity_a,relation,entity_b,origin,valid_from) "
                   "VALUES('joel','works_at','acme','org','2026-01-01')")
    from mcpbrain import config
    # curator.home is configured with role=org_curator by make_fleet; give it the pin.
    config.write_config(str(curator.home), {"org_config": {"org_pin": {
        "fleet_secret": "s3cret", "embed_model": "bge-small", "dim": 4,
        "chunker_version": "v1"}}})
    org_curate._publish(curator.store, fleet_fs, str(curator.home))  # publish snapshot v1

    # (b) a shared drive carries a cached artifact (bob's per-drive storage).
    drive_fs = LocalDirFleetStorage(tmp_path / "drive-D1")
    _publish_cache_artifact(drive_fs)

    # (c) bob bootstraps with the REAL A/B functions (no fakes), one drive D1.
    res = onboarding.bootstrap_baseline(
        bob.store, fleet_fs, ["D1"], PIN,
        make_drive_storage=lambda drive_id: drive_fs)

    # snapshot imported -> layer-1 graph present as origin='org'
    assert res["snapshot"]["status"] == "imported"
    joel = bob.store.get_entity("joel")
    assert joel is not None and joel["origin"] == "org"
    with bob.store._connect() as db:
        rel = db.execute("SELECT origin FROM entity_relations "
                         "WHERE entity_a='joel' AND relation='works_at'").fetchone()
    assert rel is not None and rel["origin"] == "org"

    # cache imported -> chunk present with ZERO local extraction (cache_hits>0)
    assert res["drives"]["D1"]["status"] == "ok"
    assert res["cache_hits"] >= 1
    assert bob.store.get_chunk("gdrive-F1-0") is not None


def test_bootstrap_ordering_snapshot_before_drives(tmp_path):
    # The contract: snapshot import is attempted before any drive cache import.
    members, curator, fleet_fs = make_fleet(tmp_path, n_members=1)
    (bob,) = members
    order = []

    def imp(store, fs):
        order.append("snapshot")
        return {"status": "no_snapshot"}

    def boot(store, fs, drive_id, pin):
        order.append(f"drive:{drive_id}")
        return {"status": "ok", "cache_hits": 0}

    onboarding.bootstrap_baseline(bob.store, fleet_fs, ["D1", "D2"], PIN,
                                  import_snapshot=imp, bootstrap_drive=boot,
                                  make_drive_storage=lambda d: fleet_fs)
    assert order[0] == "snapshot"
    assert set(order[1:]) == {"drive:D1", "drive:D2"}
