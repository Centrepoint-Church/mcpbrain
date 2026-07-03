from mcpbrain import org_contracts as oc


def test_pipeline_fingerprint_deterministic_and_sensitive():
    a = oc.pipeline_fingerprint("bge-small", 384, "v1")
    b = oc.pipeline_fingerprint("bge-small", 384, "v1")
    c = oc.pipeline_fingerprint("bge-small", 768, "v1")
    assert a == b and len(a) == 64
    assert a != c


def test_source_ref_stable_across_callers_but_secret_dependent():
    r1 = oc.source_ref("s3cret", "gdrive-abc")
    r2 = oc.source_ref("s3cret", "gdrive-abc")
    r3 = oc.source_ref("other", "gdrive-abc")
    assert r1 == r2 and len(r1) == 64
    assert r1 != r3


def test_artifact_filename_shape():
    fn = oc.artifact_filename("FID", "0123456789abcdef", "bge-small", 384, "v1")
    parts = fn.split(".")
    assert fn.endswith(".mbc.gz")
    assert parts[0] == "FID"
    assert parts[1] == "0123456789ab"          # 12 hex chars
    assert len(parts[2]) == 8                    # pipeline fp prefix


def test_cache_artifact_round_trips():
    art = oc.CacheArtifact(
        file_id="FID", content_hash="abc", extraction_method="text",
        chunker_version="v1", embed_model="bge-small", dim=384,
        chunks=(oc.CacheChunk(idx=0, text="hi", embedding_b64="AAAA", metadata={"k": 1}),),
        enrich={"logic_version": 1}, published_by="a@x.org", published_at="2026-07-03")
    d = art.to_dict()
    import json
    again = oc.CacheArtifact.from_dict(json.loads(json.dumps(d)))
    assert again == art
    assert again.chunks[0].text == "hi"


def test_contribution_record_round_trips():
    rec = oc.ContributionRecord(
        claim={"kind": "relation", "a": "joel", "rel": "works_at", "b": "acme"},
        confidence=0.9, valid_from="2026-01-01", contributor_email="a@x.org",
        source_kind="email", source_ref="deadbeef")
    import json
    again = oc.ContributionRecord.from_dict(json.loads(json.dumps(rec.to_dict())))
    assert again == rec


def test_manifest_and_tombstone_round_trip():
    m = oc.SnapshotManifest(version=3, created_at="t", entity_count=10,
                            relation_count=5, tombstone_count=1, snapshot_sha256="ff")
    assert oc.SnapshotManifest.from_dict(m.to_dict()) == m
    t = oc.Tombstone(entity_id="dup", merged_into="joel-chelliah")
    assert oc.Tombstone.from_dict(t.to_dict()) == t


def test_fleet_pin_defaults_and_is_pinned():
    empty = oc.FleetPin()
    assert empty.relation_allowlist == oc.DEFAULT_RELATION_ALLOWLIST
    assert empty.is_pinned is False
    assert oc.FleetPin(fleet_secret="x").is_pinned is True
