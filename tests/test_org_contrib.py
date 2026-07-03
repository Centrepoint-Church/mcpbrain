import json

from mcpbrain import org_contrib
from mcpbrain.org_contracts import FleetPin, ContributionRecord, source_ref
from mcpbrain.store import Store


def _store(tmp_path):
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    return s


def _pin():
    return FleetPin(fleet_secret="s3cret", relation_allowlist=("works_at", "member_of", "mentioned_with"))


def _delta(relation="works_at", *, a_type="person", b_type="org",
           a_email="joel@acme.org", origin="local", valid_to="",
           source_doc_id="msg-1"):
    return {
        "relations": [{"entity_a": "joel", "relation": relation, "entity_b": "acme",
                       "valid_from": "2026-01-01", "valid_to": valid_to,
                       "confidence": 0.9, "origin": origin,
                       "source_doc_id": source_doc_id}],
        "entities": {
            "joel": {"id": "joel", "name": "Joel Chelliah", "type": a_type,
                     "org": "Acme", "email_addr": a_email, "aliases": "",
                     "origin": origin},
            "acme": {"id": "acme", "name": "Acme", "type": b_type, "org": "",
                     "email_addr": "", "aliases": "", "origin": origin},
        },
    }


def _outbox(store):
    with store._connect() as db:
        return [json.loads(r["record"])
                for r in db.execute("SELECT record FROM org_contrib_outbox ORDER BY id").fetchall()]


def test_allowlisted_relation_contributes_edge_and_both_endpoints(tmp_path):
    s = _store(tmp_path)
    n = org_contrib.collect_from_drain(s, _delta(), _pin(), "alice@x.org")
    recs = _outbox(s)
    assert n == 3                                   # joel entity, acme entity, works_at relation
    kinds = sorted(r["claim"]["kind"] for r in recs)
    assert kinds == ["entity", "entity", "relation"]
    # source_ref is HMAC(secret, doc_id) — identical for all three, hides the doc id
    assert {r["source_ref"] for r in recs} == {source_ref("s3cret", "msg-1")}
    # NOTHING content-shaped leaks
    blob = json.dumps(recs)
    assert "msg-1" not in blob and "profile" not in blob and "mentions" not in blob


def test_unpinned_fleet_contributes_nothing(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(s, _delta(), FleetPin(), "alice@x.org") == 0
    assert _outbox(s) == []


def test_non_allowlisted_relation_dropped(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(s, _delta(relation="reports_to"), _pin(), "a@x.org") == 0


def test_role_address_endpoint_drops_whole_claim(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(
        s, _delta(a_email="office@acme.org"), _pin(), "a@x.org") == 0


def test_non_layer1_entity_type_dropped(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(
        s, _delta(b_type="document"), _pin(), "a@x.org") == 0


def test_org_origin_rows_never_re_contributed(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(s, _delta(origin="org"), _pin(), "a@x.org") == 0


def test_cold_sourced_claim_dropped(tmp_path):
    s = _store(tmp_path)
    with s._connect() as db:
        db.execute("INSERT INTO chunks(doc_id, text, content_hash, metadata, enrich_state) "
                   "VALUES('msg-cold','t','h','{}','cold')")
    assert org_contrib.collect_from_drain(
        s, _delta(source_doc_id="msg-cold"), _pin(), "a@x.org") == 0


def test_missing_provenance_fails_closed(tmp_path):
    s = _store(tmp_path)
    assert org_contrib.collect_from_drain(
        s, _delta(source_doc_id=""), _pin(), "a@x.org") == 0


def test_supersession_carries_valid_to(tmp_path):
    s = _store(tmp_path)
    org_contrib.collect_from_drain(s, _delta(valid_to="2026-06-01"), _pin(), "a@x.org")
    rel = [r for r in _outbox(s) if r["claim"]["kind"] == "relation"][0]
    assert rel["valid_to"] == "2026-06-01"


def test_records_round_trip_through_contribution_record(tmp_path):
    s = _store(tmp_path)
    org_contrib.collect_from_drain(s, _delta(), _pin(), "a@x.org")
    for raw in _outbox(s):
        assert ContributionRecord.from_dict(raw).contributor_email == "a@x.org"


def test_upload_pending_writes_one_batch_and_marks_uploaded(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    s = _store(tmp_path)
    org_contrib.collect_from_drain(s, _delta(), _pin(), "alice@x.org")
    fs = LocalDirFleetStorage(tmp_path / "fleet")
    res = org_contrib.upload_pending(s, fs, "alice@x.org")
    assert res["uploaded"] == 3
    assert res["batch"].startswith("contrib/alice@x.org/") and res["batch"].endswith(".jsonl")
    body = fs.get_bytes(res["batch"]).decode().strip().splitlines()
    assert len(body) == 3
    # second call has nothing pending
    assert org_contrib.upload_pending(s, fs, "alice@x.org") == {"uploaded": 0, "batch": ""}


def test_upload_pending_empty_is_noop(tmp_path):
    from tests.helpers.org_fleet import LocalDirFleetStorage
    s = _store(tmp_path)
    assert org_contrib.upload_pending(s, LocalDirFleetStorage(tmp_path / "f"), "a@x.org") == {
        "uploaded": 0, "batch": ""}


def test_delta_since_watermark_picks_up_new_and_superseded(tmp_path):
    from mcpbrain import graph_write
    s = _store(tmp_path)
    with s._connect() as db:
        for eid, name, typ in (("joel", "Joel", "person"), ("acme", "Acme", "org"),
                               ("beta", "Beta", "org")):
            db.execute("INSERT INTO entities(id,name,type,origin) VALUES(?,?,?,'local')",
                       (eid, name, typ))
    graph_write.upsert_relation(s, "joel", "works_at", "acme", valid_from="2026-01-01",
                                source_doc_id="msg-1")
    delta, wm = org_contrib._delta_since_watermark(s)
    rels = {(r["entity_a"], r["relation"], r["entity_b"]) for r in delta["relations"]}
    assert ("joel", "works_at", "acme") in rels
    assert "joel" in delta["entities"] and "acme" in delta["entities"]
    # Advancing the watermark then re-scanning must never surface a row we
    # haven't already seen. It MAY harmlessly re-surface an already-seen row
    # if last_seen/invalidated_at lands in the same second as the watermark's
    # own ts (the >= boundary — see test_delta_since_watermark_picks_up_same_second_*
    # for why that's required, not a bug): 1-second string timestamp
    # resolution means a strict > would silently and permanently drop same-
    # second changes, so the trade is a bounded, already-deduped-downstream
    # re-scan rather than data loss.
    s.set_meta("org_contrib_hwm", str(wm["hwm"]))
    s.set_meta("org_contrib_ts", wm["ts"])
    delta2, _ = org_contrib._delta_since_watermark(s)
    ids2 = {r["id"] for r in delta2["relations"]}
    assert ids2 <= {r["id"] for r in delta["relations"]}


def test_delta_since_watermark_excludes_stale_already_seen_relation(tmp_path):
    """Positive exclusion check: a relation already covered by hwm whose
    last_seen is strictly BEFORE the watermark's ts must NOT reappear on a
    later scan. The rescan-idempotency test's `ids2 <= {...}` assertion alone
    would also pass for a naive "return everything" regression (its fixture
    only ever has one relation); this test forces a positive negative case
    independent of wall-clock timing, so it fails if the WHERE clause is
    dropped or widened incorrectly."""
    from mcpbrain import graph_write
    s = _store(tmp_path)
    with s._connect() as db:
        for eid, name, typ in (("joel", "Joel", "person"), ("acme", "Acme", "org")):
            db.execute("INSERT INTO entities(id,name,type,origin) VALUES(?,?,?,'local')",
                       (eid, name, typ))
    rid = graph_write.upsert_relation(s, "joel", "works_at", "acme", valid_from="2026-01-01",
                                      source_doc_id="msg-1")
    delta, wm = org_contrib._delta_since_watermark(s)
    assert wm["hwm"] == rid
    s.set_meta("org_contrib_hwm", str(wm["hwm"]))
    s.set_meta("org_contrib_ts", wm["ts"])
    # Force last_seen strictly BEFORE the persisted watermark ts (unambiguously
    # stale, not a same-second collision) so this proves exclusion regardless
    # of test wall-clock timing.
    with s._connect() as db:
        db.execute("UPDATE entity_relations SET last_seen='2020-01-01T00:00:00Z' WHERE id=?",
                   (rid,))
    delta2, _ = org_contrib._delta_since_watermark(s)
    assert delta2["relations"] == []


def test_delta_since_watermark_picks_up_same_second_reobservation(tmp_path):
    """Regression for the >= fix: a re-observation (last_seen bump) landing in
    the SAME wall-clock second as the persisted watermark checkpoint must still
    be picked up. Before the fix (strict > on last_seen), a same-second bump
    failed id>hwm (row already had an id <= hwm), failed invalidated_at (NULL),
    and failed last_seen>last_ts (equal, not greater) — permanently lost, since
    the watermark only advances forward. We force the collision deterministically
    via direct SQL rather than depending on real elapsed time between calls."""
    from mcpbrain import graph_write
    s = _store(tmp_path)
    with s._connect() as db:
        for eid, name, typ in (("joel", "Joel", "person"), ("acme", "Acme", "org")):
            db.execute("INSERT INTO entities(id,name,type,origin) VALUES(?,?,?,'local')",
                       (eid, name, typ))
    rid = graph_write.upsert_relation(s, "joel", "works_at", "acme", valid_from="2026-01-01",
                                      source_doc_id="msg-1")
    delta, wm = org_contrib._delta_since_watermark(s)
    assert wm["hwm"] == rid
    collision_ts = wm["ts"]                          # the exact watermark checkpoint
    s.set_meta("org_contrib_hwm", str(wm["hwm"]))
    s.set_meta("org_contrib_ts", collision_ts)

    # Re-observe the same triple (bumps last_seen via _bump_observation), then
    # force last_seen to land EXACTLY on the watermark second — the collision
    # the bug missed, rather than relying on enough wall time passing.
    graph_write.upsert_relation(s, "joel", "works_at", "acme", valid_from="2026-01-01",
                                source_doc_id="msg-1")
    with s._connect() as db:
        db.execute("UPDATE entity_relations SET last_seen=? WHERE id=?", (collision_ts, rid))

    delta2, _ = org_contrib._delta_since_watermark(s)
    rels2 = {(r["entity_a"], r["relation"], r["entity_b"]) for r in delta2["relations"]}
    assert ("joel", "works_at", "acme") in rels2


def test_delta_since_watermark_picks_up_same_second_supersession(tmp_path):
    """Regression for the >= fix: a supersession (invalidated_at set) landing in
    the SAME wall-clock second as the persisted watermark checkpoint must still
    be picked up. works_at is a singleton relation, so upserting joel->beta
    supersedes the existing joel->acme row (graph_write._mark_superseded sets
    invalidated_at/valid_to on it) rather than leaving it untouched."""
    from mcpbrain import graph_write
    s = _store(tmp_path)
    with s._connect() as db:
        for eid, name, typ in (("joel", "Joel", "person"), ("acme", "Acme", "org"),
                               ("beta", "Beta", "org")):
            db.execute("INSERT INTO entities(id,name,type,origin) VALUES(?,?,?,'local')",
                       (eid, name, typ))
    old_rid = graph_write.upsert_relation(s, "joel", "works_at", "acme", valid_from="2026-01-01",
                                          source_doc_id="msg-1")
    delta, wm = org_contrib._delta_since_watermark(s)
    assert wm["hwm"] == old_rid
    collision_ts = wm["ts"]
    s.set_meta("org_contrib_hwm", str(wm["hwm"]))
    s.set_meta("org_contrib_ts", collision_ts)

    # Supersede joel->acme with joel->beta (newer valid_from -> the singleton
    # recency rule retires acme), then force invalidated_at to land EXACTLY on
    # the watermark second.
    graph_write.upsert_relation(s, "joel", "works_at", "beta", valid_from="2026-02-01",
                                source_doc_id="msg-2")
    with s._connect() as db:
        db.execute("UPDATE entity_relations SET invalidated_at=? WHERE id=?",
                   (collision_ts, old_rid))
        row = db.execute("SELECT invalidated_at FROM entity_relations WHERE id=?",
                         (old_rid,)).fetchone()
        assert row["invalidated_at"] == collision_ts

    delta2, _ = org_contrib._delta_since_watermark(s)
    ids2 = {r["id"] for r in delta2["relations"]}
    assert old_rid in ids2                            # superseded row picked up
    rels2 = {(r["entity_a"], r["relation"], r["entity_b"]) for r in delta2["relations"]}
    assert ("joel", "works_at", "beta") in rels2      # and the new current row too


def test_delta_since_watermark_hwm_reflects_fetched_rows_not_a_later_insert(tmp_path):
    """Regression for the second (narrower) bug: hwm must be derived from the
    rows actually fetched, not a separate MAX(id) query. Simulate a relation
    inserted after the scan's SELECT by inserting AFTER calling
    _delta_since_watermark and asserting the returned hwm does not cover it —
    so the next scan (id > hwm) still finds it, instead of it being silently
    skipped forever because a stale MAX(id) had already advanced past it."""
    from mcpbrain import graph_write
    s = _store(tmp_path)
    with s._connect() as db:
        for eid, name, typ in (("joel", "Joel", "person"), ("acme", "Acme", "org"),
                               ("beta", "Beta", "org")):
            db.execute("INSERT INTO entities(id,name,type,origin) VALUES(?,?,?,'local')",
                       (eid, name, typ))
    rid1 = graph_write.upsert_relation(s, "joel", "works_at", "acme", valid_from="2026-01-01",
                                       source_doc_id="msg-1")
    delta, wm = org_contrib._delta_since_watermark(s)
    assert wm["hwm"] == rid1
    s.set_meta("org_contrib_hwm", str(wm["hwm"]))
    s.set_meta("org_contrib_ts", wm["ts"])

    # A relation "inserted after the scan" — with the old separate-query hwm
    # this row's id would already be covered by a MAX(id) taken after it landed,
    # even though `relations` never included it. With the fix, hwm can only ever
    # be derived from rows we actually saw, so this new row's id must exceed hwm.
    rid2 = graph_write.upsert_relation(s, "joel", "mentioned_with", "beta", valid_from="2026-02-01",
                                       source_doc_id="msg-2")
    delta2, wm2 = org_contrib._delta_since_watermark(s)
    rels2 = {(r["entity_a"], r["relation"], r["entity_b"]) for r in delta2["relations"]}
    assert ("joel", "mentioned_with", "beta") in rels2
    assert wm2["hwm"] == rid2
