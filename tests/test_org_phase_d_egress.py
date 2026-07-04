import json

from mcpbrain import org_contrib, ingest_cache
from mcpbrain.org_contracts import FleetPin
from mcpbrain.store import Store
from tests.helpers.org_fleet import LocalDirFleetStorage

PIN = FleetPin(fleet_secret="s3cret",
               relation_allowlist=("works_at", "member_of", "mentioned_with"))

# Secret strings that must NEVER appear in anything uploaded to the fleet.
SECRETS = ["divorce lawyer", "my private note", "SSN 123-45-6789",
           "gdrive-secret-doc-42", "raw chunk body text"]


def _store(tmp_path):
    s = Store(tmp_path / "s.sqlite3", dim=4); s.init(); return s


def _adversarial_delta():
    # A drain delta packed with things that MUST be filtered/redacted:
    # - a person with a private annotation in name + aliases
    # - a person with NO email (must be dropped, unanchored)
    # - a non-allowlisted (sensitive) relation
    # - a role-address entity
    return {
        "relations": [
            {"entity_a": "joel", "relation": "works_at", "entity_b": "acme",
             "valid_from": "2026-01-01", "valid_to": "", "confidence": 0.9,
             "origin": "local", "source_doc_id": "gdrive-secret-doc-42"},
            {"entity_a": "joel", "relation": "has_diagnosis", "entity_b": "condition",
             "valid_from": "2026-01-01", "valid_to": "", "confidence": 0.9,
             "origin": "local", "source_doc_id": "gdrive-secret-doc-42"},
        ],
        "entities": {
            "joel": {"id": "joel", "name": "Joel (divorce lawyer)", "type": "person",
                     "org": "Acme", "email_addr": "joel@acme.org",
                     "aliases": "JC, my private note", "origin": "local",
                     "profile": "SSN 123-45-6789", "notes": "raw chunk body text"},
            "acme": {"id": "acme", "name": "Acme", "type": "org", "org": "",
                     "email_addr": "", "aliases": "", "origin": "local"},
            "condition": {"id": "condition", "name": "A Condition", "type": "person",
                          "org": "", "email_addr": "office@acme.org", "aliases": "",
                          "origin": "local"},
        },
    }


def _seed_chunk(store, doc_id, text):
    with store._connect() as db:
        db.execute("INSERT INTO chunks(doc_id,text,content_hash,metadata,enrich_state) "
                   "VALUES(?,?,?,?, '')", (doc_id, text, "h",
                   json.dumps({"source_type": "gdrive"})))


def test_no_content_shaped_data_escapes_in_contributions(tmp_path):
    s = _store(tmp_path)
    _seed_chunk(s, "gdrive-secret-doc-42", "raw chunk body text")
    org_contrib.collect_from_drain(s, _adversarial_delta(), PIN, "alice@x.org")
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    org_contrib.upload_pending(s, fs, "alice@x.org")

    # Read back exactly the bytes that left the machine.
    blobs = [fs.get_bytes(p).decode() for p in fs.list_paths("contrib/")]
    uploaded = "\n".join(blobs)

    for secret in SECRETS:
        assert secret not in uploaded, f"leaked: {secret!r}"
    # No forbidden keys anywhere in the records.
    for line in uploaded.splitlines():
        rec = json.loads(line)
        assert set(rec) <= {"claim", "confidence", "valid_from", "valid_to",
                            "contributor_email", "source_kind", "source_ref", "schema"}
        assert set(rec["claim"]) <= {"kind", "id", "name", "type", "org",
                                     "email_addr", "aliases",
                                     "entity_a", "relation", "entity_b"}
        # the raw doc id is HMAC'd, never present
        assert "gdrive-secret-doc-42" != rec["source_ref"]
    # the sensitive relation and the unanchored/role-address people are gone.
    assert "has_diagnosis" not in uploaded
    assert "condition" not in uploaded          # role-address person dropped


def test_cache_artifacts_only_written_under_cache_dir(tmp_path):
    s = _store(tmp_path)
    # a fresh, properly-embedded chunk to publish
    s.import_cached_chunk("gdrive-F1-0", "body", "vh1",
                          {"source_type": "gdrive", "file_id": "F1", "chunk_index": 0},
                          [0.1, 0.2, 0.3, 0.4])
    fs = LocalDirFleetStorage(tmp_path / "drv")
    ingest_cache.publish_file(s, fs, "D1", "F1", "vh1", PIN, published_by="p@x.org")
    paths = fs.list_paths("")
    assert paths, "expected an artifact to be published"
    assert all(p.startswith(ingest_cache.CACHE_DIR + "/") for p in paths), paths
