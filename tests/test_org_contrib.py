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
