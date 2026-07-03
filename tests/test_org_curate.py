import json

from mcpbrain import org_curate
from mcpbrain.org_contracts import ContributionRecord
from mcpbrain.store import Store


def _store(tmp_path, name="curator"):
    s = Store(tmp_path / f"{name}.sqlite3", dim=4)
    s.init()
    return s


def _rec(claim, sref="ref1", email="alice@x.org", **kw):
    return ContributionRecord(claim=claim, confidence=kw.get("confidence", 1.0),
                              valid_from=kw.get("valid_from", "2026-01-01"),
                              valid_to=kw.get("valid_to", ""), contributor_email=email,
                              source_kind="email", source_ref=sref)


def _write_batch(fs, path, recs):
    body = ("\n".join(json.dumps(r.to_dict(), sort_keys=True) for r in recs) + "\n").encode()
    fs.put_bytes(path, body)


def test_ingest_is_idempotent(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _write_batch(fs, "contrib/alice@x.org/1.jsonl",
                 [_rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person",
                        "org": "", "email_addr": "", "aliases": ""})])
    r1 = org_curate._ingest(s, fs)
    r2 = org_curate._ingest(s, fs)                 # same batch again
    assert r1["ingested"] == 1
    assert r2["ingested"] == 0                     # UNIQUE dedups
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM org_contrib_staging").fetchone()["c"] == 1


def test_ingest_counts_batches_and_multiple_new_rows(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _write_batch(fs, "contrib/alice@x.org/1.jsonl",
                 [_rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person",
                        "org": "", "email_addr": "", "aliases": ""}, sref="ref1")])
    _write_batch(fs, "contrib/bob@x.org/1.jsonl",
                 [_rec({"kind": "entity", "id": "sam", "name": "Sam", "type": "person",
                        "org": "", "email_addr": "", "aliases": ""}, sref="ref2", email="bob@x.org")])
    result = org_curate._ingest(s, fs)
    assert result == {"batches": 2, "ingested": 2}


def test_ingest_ignores_non_jsonl_files(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    fs.put_bytes("contrib/alice@x.org/readme.txt", b"not a batch")
    result = org_curate._ingest(s, fs)
    assert result == {"batches": 0, "ingested": 0}


def test_ingest_skips_malformed_lines_but_ingests_rest(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    good = _rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person",
                 "org": "", "email_addr": "", "aliases": ""})
    body = "not json\n" + json.dumps(good.to_dict(), sort_keys=True) + "\n"
    fs.put_bytes("contrib/alice@x.org/1.jsonl", body.encode())
    result = org_curate._ingest(s, fs)
    assert result == {"batches": 1, "ingested": 1}


def test_ingest_distinguishes_claims_with_same_source_ref(tmp_path):
    """UNIQUE is (contributor_email, source_ref, claim) — two different claims
    from the same source_ref/contributor must both land, not collide."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    r1 = _rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person",
               "org": "", "email_addr": "", "aliases": ""})
    r2 = _rec({"kind": "entity", "id": "joel", "name": "Joel Smith", "type": "person",
               "org": "", "email_addr": "", "aliases": ""})
    _write_batch(fs, "contrib/alice@x.org/1.jsonl", [r1, r2])
    result = org_curate._ingest(s, fs)
    assert result == {"batches": 1, "ingested": 2}
