import gzip
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


def test_ingest_skips_undecodable_batch_but_ingests_other_contributors(tmp_path):
    """One corrupt/non-UTF-8 batch file must not abort the whole run — every
    other contributor's batch in the same pass still lands."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    fs.put_bytes("contrib/alice@x.org/1.jsonl", b"\xff\xfe not valid utf-8 \x80\x81")
    good = _rec({"kind": "entity", "id": "sam", "name": "Sam", "type": "person",
                 "org": "", "email_addr": "", "aliases": ""}, email="bob@x.org")
    _write_batch(fs, "contrib/bob@x.org/1.jsonl", [good])
    result = org_curate._ingest(s, fs)
    assert result == {"batches": 1, "ingested": 1}
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM org_contrib_staging").fetchone()["c"] == 1


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


def _stage(store, recs):
    with store._connect() as db:
        for r in recs:
            db.execute(
                "INSERT OR IGNORE INTO org_contrib_staging"
                "(contributor_email, source_ref, claim, confidence, valid_from, valid_to, source_kind)"
                " VALUES(?,?,?,?,?,?,?)",
                (r.contributor_email, r.source_ref, json.dumps(r.claim, sort_keys=True),
                 r.confidence, r.valid_from, r.valid_to, r.source_kind))


def test_materialise_writes_org_rows(tmp_path):
    s = _store(tmp_path)
    _stage(s, [
        _rec({"kind": "entity", "id": "joel", "name": "Joel Chelliah", "type": "person",
              "org": "Acme", "email_addr": "joel@acme.org", "aliases": ""}),
        _rec({"kind": "entity", "id": "acme", "name": "Acme", "type": "org",
              "org": "", "email_addr": "", "aliases": ""}),
        _rec({"kind": "relation", "entity_a": "joel", "relation": "works_at", "entity_b": "acme"}),
    ])
    res = org_curate._materialise(s)
    assert res["entities"] >= 2 and res["relations"] == 1
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entities WHERE origin='org'").fetchone()["c"] >= 2
        assert db.execute("SELECT COUNT(*) c FROM entity_relations WHERE origin='org'").fetchone()["c"] == 1


def test_mentioned_with_singleton_stays_pending(tmp_path):
    s = _store(tmp_path)
    _stage(s, [
        _rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person", "org": "",
              "email_addr": "", "aliases": ""}),
        _rec({"kind": "entity", "id": "mary", "name": "Mary", "type": "person", "org": "",
              "email_addr": "", "aliases": ""}),
        _rec({"kind": "relation", "entity_a": "joel", "relation": "mentioned_with", "entity_b": "mary"}),
    ])
    res = org_curate._materialise(s)
    assert res["pending"] >= 1
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entity_relations "
                          "WHERE relation='mentioned_with'").fetchone()["c"] == 0


def test_mentioned_with_two_sources_materialises(tmp_path):
    s = _store(tmp_path)
    ents = [_rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person", "org": "",
                  "email_addr": "", "aliases": ""}, sref="r1"),
            _rec({"kind": "entity", "id": "mary", "name": "Mary", "type": "person", "org": "",
                  "email_addr": "", "aliases": ""}, sref="r1")]
    rel = {"kind": "relation", "entity_a": "joel", "relation": "mentioned_with", "entity_b": "mary"}
    _stage(s, ents + [_rec(rel, sref="r1", email="a@x.org"),
                      _rec(rel, sref="r2", email="b@x.org")])
    res = org_curate._materialise(s)
    with s._connect() as db:
        assert db.execute("SELECT COUNT(*) c FROM entity_relations "
                          "WHERE relation='mentioned_with'").fetchone()["c"] == 1


def test_adjudicate_default_is_all_pending(tmp_path):
    assert org_curate.adjudicate([{"pair_id": "a|b"}]) == []


def test_apply_merge_verdict_merges_only_on_merge(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        for eid, name in (("joel-c", "Joel C"), ("joel-chelliah", "Joel Chelliah")):
            db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                       "VALUES(?,?,'person','org',1)", (eid, name))
    res = org_curate._apply_merge_verdicts(
        s, [{"pair_id": "joel-c|joel-chelliah", "verdict": "merge", "canonical": "Joel Chelliah"}],
        cap=10)
    assert res["merged"] == 1
    assert s.get_entity("joel-c") is None or s.get_entity("joel-chelliah") is None


def test_apply_merge_verdict_pending_is_noop(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        for eid in ("a", "b"):
            db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                       "VALUES(?,?,'person','org',1)", (eid, eid))
    res = org_curate._apply_merge_verdicts(s, [{"pair_id": "a|b", "verdict": "pending"}], cap=10)
    assert res["pending"] == 1 and res["merged"] == 0
    assert s.get_entity("a") is not None and s.get_entity("b") is not None


def test_apply_merge_verdict_role_address_guarded(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('office-a','Office','person','office@x.org','org')")
        db.execute("INSERT INTO entities(id,name,type,email_addr,origin) "
                   "VALUES('office-b','Office B','person','office@y.org','org')")
    res = org_curate._apply_merge_verdicts(
        s, [{"pair_id": "office-a|office-b", "verdict": "merge"}], cap=10)
    assert res["guarded"] == 1 and res["merged"] == 0


def test_run_end_to_end_publishes_snapshot(tmp_path, monkeypatch):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    from mcpbrain.org_contracts import SnapshotManifest
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _write_batch(fs, "contrib/alice@x.org/1.jsonl", [
        _rec({"kind": "entity", "id": "joel", "name": "Joel Chelliah", "type": "person",
              "org": "Acme", "email_addr": "joel@acme.org", "aliases": ""}),
        _rec({"kind": "entity", "id": "acme", "name": "Acme", "type": "org",
              "org": "", "email_addr": "", "aliases": ""}),
        _rec({"kind": "relation", "entity_a": "joel", "relation": "works_at", "entity_b": "acme"}),
    ])
    summary = org_curate.run(s, fs, str(tmp_path))
    assert summary["published"] is True and summary["version"] == 1
    man = SnapshotManifest.from_dict(json.loads(fs.get_bytes("org-graph/manifest.json")))
    assert man.entity_count >= 2 and man.relation_count == 1
    gz = fs.get_bytes("org-graph/snapshot.jsonl.gz")
    assert hashlib_sha(gz) == man.snapshot_sha256
    lines = gzip.decompress(gz).decode().splitlines()
    kinds = {json.loads(x)["kind"] for x in lines}
    assert {"entity", "relation"} <= kinds


def hashlib_sha(b):
    import hashlib
    return hashlib.sha256(b).hexdigest()


def test_run_second_publish_bumps_version(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    _write_batch(fs, "contrib/a@x.org/1.jsonl",
                 [_rec({"kind": "entity", "id": "acme", "name": "Acme", "type": "org",
                        "org": "", "email_addr": "", "aliases": ""})])
    v1 = org_curate.run(s, fs, str(tmp_path))["version"]
    v2 = org_curate.run(s, fs, str(tmp_path))["version"]
    assert (v1, v2) == (1, 2)
