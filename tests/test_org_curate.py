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


class _FlakyGetBytesFleetStorage:
    """Wraps a real FleetStorage, raising on get_bytes for one chosen path —
    simulates a transient I/O error a real Drive-backed FleetStorage could
    legitimately throw for a single object while the rest are fine."""

    def __init__(self, inner, fail_path):
        self._inner = inner
        self._fail_path = fail_path

    def list_paths(self, prefix):
        return self._inner.list_paths(prefix)

    def get_bytes(self, path):
        if path == self._fail_path:
            raise ConnectionError("simulated transient fleet storage I/O error")
        return self._inner.get_bytes(path)

    def put_bytes(self, path, data):
        return self._inner.put_bytes(path, data)

    def delete(self, path):
        return self._inner.delete(path)


def test_ingest_skips_path_whose_get_bytes_raises_but_ingests_other_contributors(tmp_path):
    """A FleetStorage implementation raising on get_bytes for one path (a
    transient I/O error, not a data problem) must not abort ingestion of
    every other contributor's already-enumerated batch in the same pass."""
    from tests.helpers.org_fleet import LocalDirFleetStorage
    inner = LocalDirFleetStorage(tmp_path / "fleet")
    good = _rec({"kind": "entity", "id": "sam", "name": "Sam", "type": "person",
                 "org": "", "email_addr": "", "aliases": ""}, email="bob@x.org")
    _write_batch(inner, "contrib/alice@x.org/1.jsonl", [good])
    _write_batch(inner, "contrib/bob@x.org/1.jsonl", [good])
    fs = _FlakyGetBytesFleetStorage(inner, fail_path="contrib/alice@x.org/1.jsonl")
    s = _store(tmp_path)
    result = org_curate._ingest(s, fs)
    assert result == {"batches": 1, "ingested": 1}   # bob's batch still ingested
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


def test_corroboration_is_echo_safe_and_mentioned_with_needs_two_contributors():
    # Cache-echo: many contributors, ONE source doc (same HMAC) -> not corroborated
    # for a strict type (would otherwise inflate imported org data's own support).
    echo = {"srefs": {"H1"}, "contribs": {"x@a.org", "y@a.org", "z@a.org"}}
    assert org_curate._corroborated("mentioned_with", echo) is False
    # One person's two mailbox docs: 2 srefs but 1 contributor -> not enough for
    # the sensitive co-occurrence type.
    one_mailbox = {"srefs": {"H1", "H2"}, "contribs": {"x@a.org"}}
    assert org_curate._corroborated("mentioned_with", one_mailbox) is False
    # Genuinely independent: 2 sources AND 2 contributors -> corroborated.
    indep = {"srefs": {"H1", "H2"}, "contribs": {"x@a.org", "y@a.org"}}
    assert org_curate._corroborated("mentioned_with", indep) is True


def test_curator_backstop_drops_non_allowlisted_relation(tmp_path):
    """A tampered/older-version contribution carrying a non-allowlisted relation
    (and its endpoints) must NOT materialise into the org graph, even though it
    reached staging. The curator re-enforces the fleet-pinned allowlist."""
    s = _store(tmp_path)
    _stage(s, [
        _rec({"kind": "entity", "id": "alice", "name": "Alice", "type": "person",
              "org": "", "email_addr": "alice@x.org", "aliases": ""}),
        _rec({"kind": "entity", "id": "dep", "name": "Depression", "type": "person",
              "org": "", "email_addr": "", "aliases": ""}),
        _rec({"kind": "relation", "entity_a": "alice", "relation": "has_diagnosis",
              "entity_b": "dep"}),
    ])
    res = org_curate._materialise(s, pin_allowlist={"works_at", "member_of", "mentioned_with"})
    assert res["relations"] == 0
    with s._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM entity_relations "
                       "WHERE relation='has_diagnosis'").fetchone()["c"]
    assert n == 0


def test_curator_backstop_drops_non_layer1_entity_type(tmp_path):
    """An entity claim whose type isn't a layer-1 type (person/org/project) is
    dropped rather than published — defense-in-depth against a tampered type."""
    s = _store(tmp_path)
    _stage(s, [
        _rec({"kind": "entity", "id": "secret", "name": "Secret Concept",
              "type": "diagnosis", "org": "", "email_addr": "", "aliases": ""}),
    ])
    res = org_curate._materialise(s, pin_allowlist={"works_at"})
    assert res["entities"] == 0
    with s._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM entities WHERE id='secret'").fetchone()["c"]
    assert n == 0


def test_run_passes_pinned_allowlist_as_backstop(tmp_path, monkeypatch):
    """End-to-end: run() reads the fleet-pinned allowlist from config and feeds
    it to _materialise, so a non-allowlisted staged relation never publishes."""
    from mcpbrain import config
    from tests.helpers.org_fleet import LocalDirFleetStorage
    monkeypatch.setattr(config, "app_dir", lambda: tmp_path)
    home = str(tmp_path)
    config.write_config(home, {"role": "org_curator", "org_config": {"org_pin": {
        "fleet_secret": "s3cret", "relation_allowlist": ["works_at", "member_of"]}}})
    s = _store(tmp_path)
    _stage(s, [
        _rec({"kind": "entity", "id": "a", "name": "A", "type": "person",
              "org": "", "email_addr": "a@x.org", "aliases": ""}),
        _rec({"kind": "entity", "id": "b", "name": "B", "type": "person",
              "org": "", "email_addr": "b@x.org", "aliases": ""}),
        _rec({"kind": "relation", "entity_a": "a", "relation": "mentioned_with",
              "entity_b": "b"}),  # not in this fleet's pinned allowlist
    ])
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    org_curate.run(s, fs, home)
    with s._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM entity_relations "
                       "WHERE relation='mentioned_with' AND origin='org'").fetchone()["c"]
    assert n == 0


def test_rematerialise_updates_org_skeleton_with_new_info(tmp_path):
    """A curator install is also a normal member machine — its own
    resolve_entities cadence runs org<->org-guarded but org_curate._materialise
    must remain able to update its OWN org rows' skeleton on every run,
    otherwise a later-arriving email/org-change from a contributor would
    silently never reach the published entity after the first materialise."""
    s = _store(tmp_path)
    _stage(s, [_rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person",
                     "org": "", "email_addr": "", "aliases": ""})])
    org_curate._materialise(s)
    joel = s.get_entity("joel")
    assert joel["origin"] == "org" and joel["email_addr"] == ""
    # A later contribution reports joel's email — re-materialising (the
    # normal daily cadence behavior, since staging accumulates permanently
    # and is re-aggregated every run) must pick it up on the ALREADY-org row.
    _stage(s, [_rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person",
                     "org": "Acme", "email_addr": "joel@acme.org", "aliases": ""},
                    sref="ref2")])
    org_curate._materialise(s)
    joel = s.get_entity("joel")
    assert joel["origin"] == "org"
    assert joel["email_addr"] == "joel@acme.org" and joel["org"] == "Acme"


def _by_name(store, name):
    with store._connect() as db:
        row = db.execute("SELECT * FROM entities WHERE name=?", (name,)).fetchone()
    return dict(row) if row else None


def test_rematerialise_never_reverts_name_canonicalisation(tmp_path):
    """_apply_org_skeleton must only touch org/email_addr, never name/type —
    upsert_entity canonicalises name on first materialise (e.g. stripping an
    honorific), and a later re-materialise updating email must not revert
    that canonicalisation back to the raw claim string."""
    s = _store(tmp_path)
    _stage(s, [_rec({"kind": "entity", "id": "joel", "name": "Dr. Joel Chelliah",
                     "type": "person", "org": "", "email_addr": "", "aliases": ""})])
    org_curate._materialise(s)
    joel = _by_name(s, "Joel Chelliah")               # title stripped by upsert_entity
    assert joel is not None and joel["origin"] == "org"
    _stage(s, [_rec({"kind": "entity", "id": "joel", "name": "Dr. Joel Chelliah",
                     "type": "person", "org": "Acme", "email_addr": "joel@acme.org",
                     "aliases": ""}, sref="ref2")])
    org_curate._materialise(s)
    joel = _by_name(s, "Joel Chelliah")                # still stripped, not reverted
    assert joel is not None
    assert _by_name(s, "Dr. Joel Chelliah") is None    # never reverted to the raw claim string
    assert joel["email_addr"] == "joel@acme.org"       # org/email still updated


def test_tombstones_exclude_purely_local_merges(tmp_path):
    """A curator's own ordinary local-dedup merge (both sides origin='local')
    must never be published as a tombstone — publishing it would leak the
    curator's private local contacts' name-derived ids fleet-wide, bypassing
    the entire fail-closed contribution edge."""
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('john-private-donor','John Private Donor','person','local',5)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('john-donor','John Donor','person','local',2)")
    s.merge_entities("john-donor", "john-private-donor", method="deterministic")
    assert org_curate._tombstones(s) == []


def test_tombstones_include_org_winner_merges(tmp_path):
    """A merge whose winner is an org-layer row (the curator's own org<->org
    dedup, or a slug-drift/local-into-org merge) must still be published as a
    tombstone, so consumer re-imports don't resurrect the loser id."""
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('acme','Acme','org','org',5)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('acme-inc','Acme Inc','org','org',2)")
    s.merge_entities("acme-inc", "acme", method="deterministic")
    tombs = org_curate._tombstones(s)
    assert len(tombs) == 1
    assert tombs[0].entity_id == "acme-inc" and tombs[0].merged_into == "acme"


def test_tombstones_drop_merges_older_than_retention_window(tmp_path):
    """_tombstones() must not republish an org-winner merge forever — without
    a retention bound, tombstones.jsonl would re-serialise the ENTIRE org
    merge history on every single daily publish, growing unboundedly. A merge
    older than the retention window is dropped from the published list (the
    underlying entity_merge_log row itself is untouched — this only bounds
    what gets republished)."""
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('acme','Acme','org','org',5)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('acme-inc','Acme Inc','org','org',2)")
    s.merge_entities("acme-inc", "acme", method="deterministic")
    with s._connect() as db:                         # backdate the merge well past the window
        db.execute("UPDATE entity_merge_log SET at='2020-01-01 00:00:00' "
                   "WHERE winner_id='acme' AND loser_id='acme-inc'")
    assert org_curate._tombstones(s) == []
    with s._connect() as db:                         # the underlying log row itself is untouched
        assert db.execute("SELECT COUNT(*) c FROM entity_merge_log "
                          "WHERE winner_id='acme'").fetchone()["c"] == 1


def test_relation_reasserted_as_ongoing_overrides_earlier_end_date(tmp_path):
    """A later claim (higher valid_from) reasserting a relation as ongoing
    (valid_to="") must override an earlier claim's reported end date — not
    accumulate the max valid_to ever staged regardless of which claim is
    actually the most recent observation."""
    s = _store(tmp_path)
    _stage(s, [
        _rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person",
              "org": "", "email_addr": "", "aliases": ""}),
        _rec({"kind": "entity", "id": "acme", "name": "Acme", "type": "org",
              "org": "", "email_addr": "", "aliases": ""}),
        _rec({"kind": "relation", "entity_a": "joel", "relation": "works_at", "entity_b": "acme"},
             valid_from="2026-01-01", valid_to="2026-06-01"),          # early: reports an end date
        _rec({"kind": "relation", "entity_a": "joel", "relation": "works_at", "entity_b": "acme"},
             valid_from="2026-07-01", valid_to="", sref="ref2"),       # later: still ongoing
    ])
    org_curate._materialise(s)
    with s._connect() as db:
        row = db.execute("SELECT valid_to FROM entity_relations "
                         "WHERE entity_a='joel' AND relation='works_at' AND entity_b='acme'").fetchone()
    assert row["valid_to"] in (None, "")     # the later, ongoing claim wins


def test_relation_supersession_not_clobbered_by_contributed_valid_to(tmp_path):
    """When a singleton relation (works_at) is naturally superseded by a newer
    triple (joel now works_at beta instead of acme), that supersession's own
    valid_to (the newer job's start date) must survive — a contributed
    end-date claim for the OLD triple must not overwrite it after the fact,
    regardless of which triple happens to be processed first."""
    s = _store(tmp_path)
    _stage(s, [
        _rec({"kind": "entity", "id": "joel", "name": "Joel", "type": "person",
              "org": "", "email_addr": "", "aliases": ""}),
        _rec({"kind": "entity", "id": "acme", "name": "Acme", "type": "org",
              "org": "", "email_addr": "", "aliases": ""}),
        _rec({"kind": "entity", "id": "beta", "name": "Beta", "type": "org",
              "org": "", "email_addr": "", "aliases": ""}),
        # beta staged (and therefore processed) BEFORE acme, so upsert_relation's
        # own supersession fires on acme mid-pass-1, before _materialise's own
        # contributed-valid_to write for acme ever runs — the exact ordering
        # that exposed the historical stomping bug (pre-fix: a single-pass,
        # unconditional write let this contributed value overwrite whatever
        # upsert_relation had just decided).
        _rec({"kind": "relation", "entity_a": "joel", "relation": "works_at", "entity_b": "beta"},
             valid_from="2026-06-01"),                          # newer job supersedes acme
        _rec({"kind": "relation", "entity_a": "joel", "relation": "works_at", "entity_b": "acme"},
             valid_from="2026-01-01", valid_to="2026-03-01", sref="ref2"),   # contributed end date for the OLD job
    ])
    org_curate._materialise(s)
    with s._connect() as db:
        acme_rel = db.execute("SELECT valid_to FROM entity_relations "
                              "WHERE entity_a='joel' AND relation='works_at' AND entity_b='acme'").fetchone()
        beta_rel = db.execute("SELECT valid_to FROM entity_relations "
                              "WHERE entity_a='joel' AND relation='works_at' AND entity_b='beta'").fetchone()
    # graph_write's own supersession cascade retires acme at beta's start date —
    # that decision survives, not the earlier-contributed 2026-03-01.
    assert acme_rel["valid_to"] == "2026-06-01"
    assert beta_rel["valid_to"] in (None, "")    # beta is the current job


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


def test_apply_org_merge_answers_merges_on_same_true(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('joel','Joel','person','org',1)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('joel-chelliah','Joel Chelliah','person','org',5)")
    res = org_curate.apply_org_merge_answers(
        s, [{"pair_id": "joel|joel-chelliah", "same": True, "canonical": "Joel Chelliah"}],
        cap=10)
    assert res["merged"] == 1
    survivors = [e for e in (s.get_entity("joel"), s.get_entity("joel-chelliah")) if e]
    assert len(survivors) == 1


def test_apply_org_merge_answers_same_false_suppresses_and_does_not_merge(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('sam','Sam Lee','person','org',1)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('samuel','Samuel Lee','person','org',1)")
    res = org_curate.apply_org_merge_answers(
        s, [{"pair_id": "sam|samuel", "same": False, "canonical": ""}], cap=10)
    assert res["merged"] == 0 and res["distinct"] == 1
    assert s.get_entity("sam") is not None and s.get_entity("samuel") is not None
    # suppressed -> _build_adjudication_units must never re-queue this pair
    assert "sam|samuel" in org_curate._suppressed_pairs(s)


def test_suppressed_pair_not_requeued(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('sam','Sam Lee','person','org',1)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('samuel','Samuel Lee','person','org',1)")
    before = {u["pair_id"] for u in org_curate._build_adjudication_units(s)}
    assert "sam|samuel" in before                      # a genuine fuzzy candidate
    org_curate.apply_org_merge_answers(
        s, [{"pair_id": "sam|samuel", "same": False}], cap=10)
    after = {u["pair_id"] for u in org_curate._build_adjudication_units(s)}
    assert "sam|samuel" not in after                   # never re-asked


def test_apply_org_merge_answers_respects_cap(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('joel','Joel','person','org',1)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('joel-chelliah','Joel Chelliah','person','org',5)")
    res = org_curate.apply_org_merge_answers(
        s, [{"pair_id": "joel|joel-chelliah", "same": True}], cap=0)
    assert res["capped"] == 1 and res["merged"] == 0
    assert s.get_entity("joel") is not None and s.get_entity("joel-chelliah") is not None


def test_run_returns_adjudication_units_for_the_daemon_to_stash(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('joel','Joel','person','org',1)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('joel-chelliah','Joel Chelliah','person','org',5)")
    summary = org_curate.run(s, fs, str(tmp_path))
    pids = {u["pair_id"] for u in summary["adjudication_units"]}
    assert "joel|joel-chelliah" in pids
    # units are structural-only: no content-shaped fields
    u = summary["adjudication_units"][0]
    assert set(u["a"]) <= {"id", "name", "type", "email_addr", "aliases"}


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


def test_drain_org_merge_review_applies_via_registry(tmp_path):
    """The drain BLOCK_DRAINERS entry routes pushed org_merge_review answers to
    the curator applier (the async apply path)."""
    from mcpbrain import drain
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('joel','Joel','person','org',1)")
        db.execute("INSERT INTO entities(id,name,type,origin,mentions) "
                   "VALUES('joel-chelliah','Joel Chelliah','person','org',5)")
    res = drain.BLOCK_DRAINERS["org_merge_review"](
        s, {"org_merge_review": [{"pair_id": "joel|joel-chelliah", "same": True}]})
    assert res["merged"] == 1
    survivors = [e for e in (s.get_entity("joel"), s.get_entity("joel-chelliah")) if e]
    assert len(survivors) == 1
