from mcpbrain.store import Store
from mcpbrain import org_contrib
from mcpbrain.org_contracts import FleetPin   # NOT mcpbrain.fleet


def _store(tmp_path):
    # Store(...) does NOT create its schema; .init() does. Without it the very
    # first query fails with "no such table".
    s = Store(str(tmp_path / "brain.sqlite3"), dim=8)
    s.init()
    return s


def _pin():
    # relation_allowlist is a tuple on FleetPin (org_contracts.py:142).
    return FleetPin(fleet_secret="s" * 32,
                    relation_allowlist=("works_at", "member_of"))


def test_source_kind_maps_anarlog_to_meeting(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("anarlog-a-summary-0", "text", "h1",
                   {"source_type": "anarlog", "session_id": "a"})
    assert org_contrib._source_kind(s, "anarlog-a-summary-0") == "meeting"


def test_meeting_sourced_relation_never_contributes(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("anarlog-a-summary-0", "text", "h1",
                   {"source_type": "anarlog", "session_id": "a"})
    delta = {
        "relations": [{"entity_a": "dana-okafor", "relation": "works_at",
                       "entity_b": "northgate-trust", "origin": "local",
                       "source_doc_id": "anarlog-a-summary-0",
                       "valid_from": "2026-09-17", "confidence": 1.0}],
        "entities": {
            "dana-okafor": {"id": "dana-okafor", "name": "Dana Okafor",
                            "type": "person", "origin": "local",
                            "email_addr": "dana@northgate.example"},
            "northgate-trust": {"id": "northgate-trust", "name": "Northgate Trust",
                                "type": "org", "origin": "local"},
        },
    }
    assert org_contrib.collect_from_drain(s, delta, _pin(), "me@example.com") == 0


def test_non_meeting_relation_still_contributes(tmp_path):
    s = _store(tmp_path)
    s.upsert_chunk("gmail-1", "text", "h1", {"source_type": "gmail"})
    delta = {
        "relations": [{"entity_a": "dana-okafor", "relation": "works_at",
                       "entity_b": "northgate-trust", "origin": "local",
                       "source_doc_id": "gmail-1",
                       "valid_from": "2026-09-17", "confidence": 1.0}],
        "entities": {
            "dana-okafor": {"id": "dana-okafor", "name": "Dana Okafor",
                            "type": "person", "origin": "local",
                            "email_addr": "dana@northgate.example"},
            "northgate-trust": {"id": "northgate-trust", "name": "Northgate Trust",
                                "type": "org", "origin": "local"},
        },
    }
    assert org_contrib.collect_from_drain(s, delta, _pin(), "me@example.com") > 0
