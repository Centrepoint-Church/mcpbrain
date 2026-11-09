"""console_stats: digest numbers, freshness and health (pure, over sqlite)."""
import itertools
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from mcpbrain import console_stats as cs

NOW = datetime(2026, 11, 9, 9, 40, tzinfo=timezone.utc)


def _db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    return db


def _chunks(db, rows):
    db.execute("CREATE TABLE chunks(rowid INTEGER PRIMARY KEY, enrich_state TEXT, enriched INTEGER)")
    db.executemany("INSERT INTO chunks(enrich_state, enriched) VALUES(?,?)", rows)


# --- index_counts / pct_of ---------------------------------------------------

def test_index_counts_exact_split():
    db = _db()
    _chunks(db, [("cold", 1)] * 6 + [("cold", 0)] * 4 + [("", 1)] * 3 + [("", 0)])
    assert cs.index_counts(db) == {"total": 14, "eligible": 4, "cold": 10, "duplicate": 0,
                                   "enriched_eligible": 3, "waiting": 1, "pct": 75}


def test_pct_999_of_1000_is_99():
    assert cs.pct_of(999, 1000) == 99


def test_pct_all_enriched_is_100():
    assert cs.pct_of(5, 5) == 100


def test_pct_no_eligible_is_zero():
    assert cs.pct_of(0, 0) == 0
    db = _db()
    _chunks(db, [("cold", 1)] * 3)
    assert cs.index_counts(db)["pct"] == 0


def test_every_state_enriched_combination_is_in_bounds():
    states = [None, "", "cold", "duplicate", "other"]
    combos = list(itertools.product(states, [0, 1, None]))
    for n in range(1, 4):
        for subset in itertools.combinations(combos, n):
            db = _db()
            _chunks(db, list(subset))
            r = cs.index_counts(db)
            assert r["waiting"] >= 0
            assert 0 <= r["pct"] <= 100
            db.close()


def test_duplicates_count_as_eligible():
    db = _db()
    _chunks(db, [("duplicate", 1), ("", 1)])
    r = cs.index_counts(db)
    assert (r["eligible"], r["duplicate"], r["pct"]) == (2, 1, 100)


def test_index_counts_missing_table_is_zeros():
    assert cs.index_counts(_db()) == {"total": 0, "eligible": 0, "cold": 0, "duplicate": 0,
                                      "enriched_eligible": 0, "waiting": 0, "pct": 0}


# --- graph_counts ------------------------------------------------------------

def _graph(db, *, invalidated=True):
    db.execute("CREATE TABLE entities(id TEXT PRIMARY KEY, name TEXT, type TEXT)")
    inv = ", invalidated_at TEXT" if invalidated else ""
    db.execute(f"CREATE TABLE entity_relations(id INTEGER PRIMARY KEY, entity_a TEXT, entity_b TEXT{inv})")
    db.execute(f"CREATE TABLE entity_observations(id INTEGER PRIMARY KEY, entity_id TEXT{inv})")


def test_graph_counts_type_keys_always_present():
    r = cs.graph_counts(_db())
    assert set(r["by_type"]) == {"person", "org", "project", "topic", "meeting", "event",
                                 "document", "thread", "unknown"}
    assert all(v == 0 for v in r["by_type"].values())
    assert (r["relations_live"], r["observations_live"], r["unlinked"]) == (0, 0, 0)


def test_graph_counts_null_and_unrecognised_type_go_to_unknown():
    db = _db()
    _graph(db)
    db.executemany("INSERT INTO entities VALUES(?,?,?)", [
        ("acme-a", "Dana Okafor", "person"), ("acme-b", "Acme", "org"),
        ("acme-c", "X", None), ("acme-d", "Y", "gizmo")])
    r = cs.graph_counts(db)
    assert r["by_type"]["person"] == 1 and r["by_type"]["org"] == 1
    assert r["by_type"]["unknown"] == 2


def test_graph_counts_excludes_invalidated_and_counts_unlinked():
    db = _db()
    _graph(db)
    db.executemany("INSERT INTO entities VALUES(?,?,?)", [
        ("acme-a", "Dana Okafor", "person"), ("acme-b", "Acme", "org"),
        ("acme-c", "Sam Chen", "person"), ("acme-d", "Lone", "topic")])
    db.execute("INSERT INTO entity_relations(entity_a, entity_b) VALUES('acme-a','acme-b')")
    db.execute("INSERT INTO entity_relations(entity_a, entity_b, invalidated_at) "
               "VALUES('acme-c','acme-b','2026-11-01')")
    db.execute("INSERT INTO entity_observations(entity_id) VALUES('acme-a')")
    db.execute("INSERT INTO entity_observations(entity_id, invalidated_at) VALUES('acme-a','2026-11-01')")
    r = cs.graph_counts(db)
    assert r["relations_live"] == 1
    assert r["observations_live"] == 1
    assert r["unlinked"] == 2  # acme-c (only invalidated) and acme-d (none)


def test_graph_counts_without_invalidated_column_counts_all_live():
    db = _db()
    _graph(db, invalidated=False)
    db.execute("INSERT INTO entities VALUES('acme-a','A','person')")
    db.execute("INSERT INTO entities VALUES('acme-b','B','org')")
    db.execute("INSERT INTO entity_relations(entity_a, entity_b) VALUES('acme-a','acme-b')")
    db.execute("INSERT INTO entity_observations(entity_id) VALUES('acme-a')")
    r = cs.graph_counts(db)
    assert (r["relations_live"], r["observations_live"], r["unlinked"]) == (1, 1, 0)


# --- parse_utc ---------------------------------------------------------------

def test_parse_utc_sqlite_format():
    assert cs.parse_utc("2026-11-06 01:40:03").isoformat() == "2026-11-06T01:40:03+00:00"


def test_parse_utc_iso_offset_round_trips():
    assert cs.parse_utc("2026-11-06T01:40:03+00:00").isoformat() == "2026-11-06T01:40:03+00:00"
    assert cs.parse_utc("2026-11-06T09:40:03+08:00").isoformat() == "2026-11-06T01:40:03+00:00"


def test_parse_utc_garbage_is_none():
    assert cs.parse_utc("not a date") is None
    assert cs.parse_utc(None) is None
    assert cs.parse_utc("") is None


# --- freshness ---------------------------------------------------------------

def _cursors(db, rows):
    db.execute("CREATE TABLE sync_cursors(source TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT)")
    db.executemany("INSERT INTO sync_cursors(source, cursor, updated_at) VALUES(?,?,?)", rows)


def test_source_times_ignores_shared_drive_and_backfill_keys():
    db = _db()
    _cursors(db, [
        ("gmail", "1", "2026-11-09 09:00:00"),
        ("drive", "t", "2026-11-09T08:00:00+00:00"),
        ("drive:0AFAKE", "t", "2020-01-01 00:00:00"),
        ("gmail_backfill_until", "x", "2019-01-01 00:00:00"),
        ("drive_backfill_empty", "3", "2019-01-01 00:00:00"),
    ])
    t = cs.source_times(db)
    assert set(t) == set(cs.SOURCE_KEYS)
    assert t["gmail"] == "2026-11-09T09:00:00+00:00"
    assert t["drive"] == "2026-11-09T08:00:00+00:00"
    assert t["calendar"] is None and t["anarlog"] is None


def test_shared_drive_health_flags_old_cursor_as_stale():
    db = _db()
    _cursors(db, [
        ("drive:0AFAKE", "t", "2026-07-09 00:00:00"),
        ("drive:0AFRESH", "t", "2026-11-09 09:00:00"),
        ("drive:0AFAKE_backfill_until", "x", "2019-01-01 00:00:00"),
        ("drive", "t", "2026-11-09 09:00:00"),
    ])
    assert cs.shared_drive_health(db, NOW) == {"total": 2, "stale": 1}


def test_enrich_activity_counts_last_24h_across_formats():
    db = _db()
    db.execute("CREATE TABLE enrich_payloads(file_id TEXT PRIMARY KEY, at TEXT)")
    db.executemany("INSERT INTO enrich_payloads VALUES(?,?)", [
        ("a", "2026-11-09 01:40:03"),
        ("b", "2026-11-08T23:00:00+00:00"),
        ("c", "2026-11-06 09:00:00"),
    ])
    r = cs.enrich_activity(db, NOW)
    assert r["pushes_24h"] == 2
    assert r["last_push_at"] == "2026-11-09T01:40:03+00:00"


def test_enrich_activity_missing_table():
    assert cs.enrich_activity(_db(), NOW) == {"last_push_at": None, "pushes_24h": 0}


# --- health ------------------------------------------------------------------

def _src(label="Gmail", hours=None):
    when = None if hours is None else (NOW - timedelta(hours=hours)).isoformat()
    return {"name": label.lower(), "label": label, "last_synced_at": when}


def _h(**over):
    base = dict(now=NOW, paused=False, stalled=None, watchdog_limit_reached=False,
                connections=[], open_findings=0, pending_reviews=0,
                sources=[_src(hours=0.1)], waiting=0, last_push_at=None)
    base.update(over)
    return cs.health(**base)


@pytest.mark.parametrize("over,state", [
    (dict(paused=True, stalled="cycle", open_findings=3, waiting=9), "paused"),
    (dict(stalled="cycle"), "stalled"),
    (dict(watchdog_limit_reached=True), "stalled"),
    (dict(sources=[_src(hours=7)]), "stalled"),
    (dict(sources=[_src(hours=None)]), "healthy"),
    (dict(open_findings=2), "needs_you"),
    (dict(pending_reviews=1), "needs_you"),
    (dict(connections=[{"name": "google", "label": "Google", "state": "needs_action"}]),
     "needs_you"),
    (dict(waiting=3578, last_push_at=(NOW - timedelta(hours=14)).isoformat()), "behind"),
    (dict(waiting=0, last_push_at=None), "healthy"),
    (dict(waiting=3578, last_push_at=(NOW - timedelta(hours=1)).isoformat()), "healthy"),
    (dict(open_findings=1, waiting=3578,
          last_push_at=(NOW - timedelta(hours=14)).isoformat()), "needs_you"),
])
def test_health_states(over, state):
    assert _h(**over)["state"] == state


def test_health_stale_reason_text():
    r = _h(sources=[_src("Gmail", hours=7.2)])
    assert r["reasons"] == ["Gmail has not synced for 7 hours"]


# --- stats() -----------------------------------------------------------------

def test_stats_top_level_keys(tmp_path):
    p = tmp_path / "b.sqlite3"
    with sqlite3.connect(str(p)):
        pass

    class S:
        _path = str(p)

        def list_communities(self):
            return []

        def pending_corrections(self):
            return []

    out = cs.stats(S(), str(tmp_path), {}, now=NOW)
    assert list(out) == ["as_of", "index", "enrich", "graph", "sources", "health",
                         "needs_you", "connections", "system"]
    assert [s["name"] for s in out["sources"]] == list(cs.SOURCE_KEYS)


def test_graph_counts_entities_without_type_column_are_unknown():
    db = _db()
    db.execute("CREATE TABLE entities(id INTEGER PRIMARY KEY)")
    db.execute("INSERT INTO entities DEFAULT VALUES")
    assert cs.graph_counts(db)["by_type"]["unknown"] == 1


def test_switched_off_source_is_never_stale_or_stalled(tmp_path):
    """A source switched off in Settings keeps its old cursor: it must not turn
    the console red. It is still listed, with enabled False."""
    import json
    (tmp_path / "config.json").write_text(json.dumps(
        {"sources": {"calendar": False}, "anarlog": {"enabled": False}}))
    p = tmp_path / "b.sqlite3"
    old = (NOW - timedelta(hours=9)).strftime("%Y-%m-%dT%H:%M:%SZ")
    fresh = (NOW - timedelta(minutes=5)).strftime("%Y-%m-%dT%H:%M:%SZ")
    with sqlite3.connect(str(p)) as db:
        db.execute("CREATE TABLE sync_cursors(source TEXT PRIMARY KEY, cursor TEXT, updated_at TEXT)")
        db.executemany("INSERT INTO sync_cursors VALUES (?,?,?)",
                       [("gmail", "x", fresh), ("drive", "x", fresh),
                        ("calendar", "x", old), ("anarlog", "x", old)])

    class S:
        _path = str(p)

        def list_communities(self):
            return []

        def pending_corrections(self):
            return []

    out = cs.stats(S(), str(tmp_path), {}, now=NOW)
    by = {s["name"]: s for s in out["sources"]}
    assert by["calendar"]["enabled"] is False and by["calendar"]["stale"] is False
    assert by["anarlog"]["enabled"] is False and by["anarlog"]["stale"] is False
    assert by["gmail"]["enabled"] is True
    assert out["health"]["state"] != "stalled"

    # Switched back on, the same old cursor is stalled again.
    (tmp_path / "config.json").write_text(json.dumps({"sources": {"calendar": True}}))
    out = cs.stats(S(), str(tmp_path), {}, now=NOW)
    assert {s["name"]: s for s in out["sources"]}["calendar"]["stale"] is True
    assert out["health"]["state"] == "stalled"
