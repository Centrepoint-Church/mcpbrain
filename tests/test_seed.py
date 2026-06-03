"""Tests for bin/seed_from_nexus.py — seeding mcpbrain's graph from Nexus.

The seed copies entities, bitemporal relations, projects, and areas out of a
Nexus memory DB into a fresh mcpbrain store. These tests build a tiny fake
Nexus sqlite (Nexus-shaped tables, hand-inserted rows) in a tmp file so they
run fully offline — no real Nexus DB is touched.
"""
import importlib.util
import sqlite3
from pathlib import Path

from mcpbrain.store import Store

# bin/ is not a package; load seed_from_nexus by file path.
_SEED_PATH = Path(__file__).resolve().parents[1] / "bin" / "seed_from_nexus.py"
_spec = importlib.util.spec_from_file_location("seed_from_nexus", _SEED_PATH)
seed_mod = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(seed_mod)
seed = seed_mod.seed


def _make_nexus_db(path: Path) -> None:
    """Create the Nexus-shaped entities + entity_relations tables (no rows)."""
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE entities (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
        org TEXT DEFAULT '', email_addr TEXT DEFAULT '', aliases TEXT DEFAULT '',
        first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '',
        email_count INTEGER DEFAULT 0, notes TEXT DEFAULT '',
        summary TEXT DEFAULT '', summary_updated TEXT DEFAULT '',
        needs_resynthesis INTEGER DEFAULT 0, degree INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE entity_relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_a TEXT NOT NULL, relation TEXT NOT NULL, entity_b TEXT NOT NULL,
        valid_from TEXT, valid_to TEXT, created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        invalidated_at TEXT, invalidated_by_relation_id INTEGER,
        superseded_reason TEXT, confidence REAL DEFAULT 1.0, evidence TEXT,
        strength INTEGER DEFAULT 1, normalised_strength REAL DEFAULT 0.0,
        since TEXT, last_seen TEXT)""")
    db.commit()
    db.close()


def _make_nexus_projects_areas(path: Path) -> None:
    """Add the Nexus-shaped projects + areas tables to an existing fake DB."""
    db = sqlite3.connect(path)
    db.execute("""CREATE TABLE projects (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, org_tag TEXT, status_line TEXT,
        status_updated_at TEXT, created_at TEXT, archived_at TEXT, notes_path TEXT,
        outcome TEXT, status TEXT DEFAULT 'active', target_date TEXT,
        actual_done_date TEXT, priority TEXT, area_id TEXT, owner_entity_id TEXT,
        updated_at TEXT)""")
    db.execute("""CREATE TABLE areas (
        id TEXT PRIMARY KEY, org_id TEXT NOT NULL, name TEXT NOT NULL,
        description TEXT, standard TEXT, review_cadence TEXT, last_reviewed_at TEXT,
        active INTEGER NOT NULL DEFAULT 1, created_at TEXT, archived_at TEXT)""")
    db.commit()
    db.close()


def _mcpbrain_store(tmp_path) -> Store:
    s = Store(tmp_path / "brain.sqlite3", dim=4)
    s.init()
    return s


# --- 7.1: entities + relations ----------------------------------------------

def test_seed_entities_and_relations(tmp_path):
    nexus = tmp_path / "nexus_memory.sqlite3"
    _make_nexus_db(nexus)
    db = sqlite3.connect(nexus)
    db.execute(
        "INSERT INTO entities(id,name,type,org,email_addr,aliases,first_seen,"
        "last_seen,email_count,notes,degree) VALUES "
        "('taryn-hamilton','Taryn Hamilton','person','Centrepoint',"
        "'taryn@cp.church','Taz','2025-01-01','2025-06-01',12,'exec',5)")
    db.execute(
        "INSERT INTO entities(id,name,type,org,email_addr,aliases,first_seen,"
        "last_seen,email_count,notes,degree) VALUES "
        "('joel-chelliah','Joel Chelliah','person','Centrepoint',"
        "'joel@cp.church','','2025-02-01','2025-05-01',8,'',3)")
    db.execute(
        "INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from,"
        "valid_to,invalidated_at,confidence,evidence,strength,since,last_seen) "
        "VALUES ('taryn-hamilton','reports_to','joel-chelliah','2025-01-01',"
        "'2025-12-31',NULL,0.9,'org chart',4,'2025-01-01','2025-06-01')")
    db.commit()
    db.close()

    store = _mcpbrain_store(tmp_path)
    summary = seed(str(nexus), store)

    ents = {e["id"]: e for e in store.list_entities()}
    assert set(ents) == {"taryn-hamilton", "joel-chelliah"}
    assert ents["taryn-hamilton"]["org"] == "Centrepoint"
    assert ents["taryn-hamilton"]["degree"] == 5
    assert ents["taryn-hamilton"]["email_count"] == 12
    assert ents["taryn-hamilton"]["aliases"] == "Taz"
    assert ents["taryn-hamilton"]["email_addr"] == "taryn@cp.church"

    rels = store.list_relations()
    assert len(rels) == 1
    r = rels[0]
    assert (r["entity_a"], r["relation"], r["entity_b"]) == (
        "taryn-hamilton", "reports_to", "joel-chelliah")
    assert r["valid_from"] == "2025-01-01"
    assert r["valid_to"] == "2025-12-31"
    assert r["invalidated_at"] is None
    assert r["confidence"] == 0.9
    assert r["strength"] == 4

    assert summary["entities"] == 2
    assert summary["relations"] == 1


def test_seed_idempotent(tmp_path):
    nexus = tmp_path / "nexus_memory.sqlite3"
    _make_nexus_db(nexus)
    db = sqlite3.connect(nexus)
    db.execute(
        "INSERT INTO entities(id,name,type,org) VALUES "
        "('a','A','person','ACC'),('b','B','person','ACC')")
    db.execute(
        "INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from) "
        "VALUES ('a','knows','b','2025-01-01')")
    db.commit()
    db.close()

    store = _mcpbrain_store(tmp_path)
    seed(str(nexus), store)
    seed(str(nexus), store)

    assert len(store.list_entities()) == 2
    assert len(store.list_relations()) == 1


def test_seed_carries_temporal(tmp_path):
    nexus = tmp_path / "nexus_memory.sqlite3"
    _make_nexus_db(nexus)
    db = sqlite3.connect(nexus)
    db.execute(
        "INSERT INTO entities(id,name,type) VALUES "
        "('a','A','person'),('b','B','person')")
    db.execute(
        "INSERT INTO entity_relations(entity_a,relation,entity_b,valid_from,"
        "valid_to,invalidated_at,superseded_reason) VALUES "
        "('a','reports_to','b','2024-01-01','2024-06-01','2024-06-01T00:00:00Z',"
        "'role changed')")
    db.commit()
    db.close()

    store = _mcpbrain_store(tmp_path)
    seed(str(nexus), store)

    rels = store.list_relations()
    assert len(rels) == 1
    assert rels[0]["invalidated_at"] == "2024-06-01T00:00:00Z"
    assert rels[0]["valid_to"] == "2024-06-01"
    assert rels[0]["superseded_reason"] == "role changed"

    # Re-seed must not resurrect it as valid.
    seed(str(nexus), store)
    rels = store.list_relations()
    assert len(rels) == 1
    assert rels[0]["invalidated_at"] == "2024-06-01T00:00:00Z"


def test_seed_entities_missing_column_uses_default(tmp_path):
    """A Nexus entities table missing a column (schema drift) must not crash;
    the entity is seeded with the sensible default (email_count -> 0)."""
    nexus = tmp_path / "nexus_memory.sqlite3"
    db = sqlite3.connect(nexus)
    # Nexus-shaped entities table with no email_count and no aliases column.
    db.execute("""CREATE TABLE entities (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, type TEXT NOT NULL,
        org TEXT DEFAULT '', email_addr TEXT DEFAULT '',
        first_seen TEXT DEFAULT '', last_seen TEXT DEFAULT '',
        notes TEXT DEFAULT '', degree INTEGER DEFAULT 0)""")
    db.execute("""CREATE TABLE entity_relations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        entity_a TEXT NOT NULL, relation TEXT NOT NULL, entity_b TEXT NOT NULL,
        valid_from TEXT, valid_to TEXT, invalidated_at TEXT,
        invalidated_by_relation_id INTEGER, superseded_reason TEXT,
        confidence REAL DEFAULT 1.0, evidence TEXT, strength INTEGER DEFAULT 1,
        normalised_strength REAL DEFAULT 0.0, since TEXT, last_seen TEXT)""")
    db.execute(
        "INSERT INTO entities(id,name,type,org) VALUES "
        "('drifty','Drifty Person','person','ACC')")
    db.commit()
    db.close()

    store = _mcpbrain_store(tmp_path)
    summary = seed(str(nexus), store)

    ents = {e["id"]: e for e in store.list_entities()}
    assert set(ents) == {"drifty"}
    assert ents["drifty"]["org"] == "ACC"
    assert ents["drifty"]["email_count"] == 0
    assert ents["drifty"]["aliases"] == ""
    assert summary["entities"] == 1


# --- 7.2: projects + areas ---------------------------------------------------

def test_seed_projects_and_areas(tmp_path):
    nexus = tmp_path / "nexus_memory.sqlite3"
    _make_nexus_db(nexus)
    _make_nexus_projects_areas(nexus)
    db = sqlite3.connect(nexus)
    db.execute(
        "INSERT INTO areas(id,org_id,name,description,active,archived_at) VALUES "
        "('area-ops','3','Operations','Ops area',1,NULL),"
        "('area-old','3','Retired','done',0,'2025-01-01')")
    db.execute(
        "INSERT INTO projects(id,name,org_tag,status_line,status,created_at,"
        "archived_at,area_id,owner_entity_id) VALUES "
        "('proj-cams','CAMS Review','ACC','in flight','active','2025-01-01',"
        "NULL,'area-ops','taryn-hamilton'),"
        "('proj-done','Old Project','Centrepoint','wrapped','done','2024-01-01',"
        "'2024-12-01','area-ops',NULL)")
    db.commit()
    db.close()

    store = _mcpbrain_store(tmp_path)
    summary = seed(str(nexus), store)

    cams = store.get_project("proj-cams")
    assert cams["name"] == "CAMS Review"
    assert cams["org_tag"] == "ACC"
    assert cams["status"] == "active"
    assert cams["archived_at"] is None
    assert cams["area_id"] == "area-ops"
    assert cams["owner_entity_id"] == "taryn-hamilton"

    done = store.get_project("proj-done")
    assert done["archived_at"] == "2024-12-01"

    ops = store.get_area("area-ops")
    assert ops["name"] == "Operations"
    assert ops["org_id"] == "3"
    assert ops["active"] == 1

    old = store.get_area("area-old")
    assert old["active"] == 0
    assert old["archived_at"] == "2025-01-01"

    assert summary["projects"] == 2
    assert summary["areas"] == 2


def test_seed_projects_areas_idempotent(tmp_path):
    nexus = tmp_path / "nexus_memory.sqlite3"
    _make_nexus_db(nexus)
    _make_nexus_projects_areas(nexus)
    db = sqlite3.connect(nexus)
    db.execute("INSERT INTO areas(id,org_id,name,active) VALUES ('a1','3','A',1)")
    db.execute(
        "INSERT INTO projects(id,name,org_tag,status) VALUES "
        "('p1','P','ACC','active')")
    db.commit()
    db.close()

    store = _mcpbrain_store(tmp_path)
    seed(str(nexus), store)
    seed(str(nexus), store)

    with store._connect() as conn:
        assert conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM areas").fetchone()[0] == 1
