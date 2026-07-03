"""One-shot migration logic (Boulder 3, Task 11) and the attended CLI (Task 12)."""

import importlib.util
import pathlib

import pytest

from mcpbrain import consolidate
from mcpbrain.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "brain.db", dim=4)
    s.init()
    return s


def test_remap_topics_folds_variants(store, tmp_path):
    home = str(tmp_path)
    store.upsert_entity("topic-budgets", "budgets", "topic", "", "2026-01-01")
    store.upsert_entity("topic-budget", "budget", "topic", "", "2026-01-01")
    out = consolidate.remap_topics(store, home)
    with store._connect() as db:
        ids = [r["id"] for r in db.execute("SELECT id FROM entities WHERE type='topic'").fetchall()]
    assert ids == ["topic-budget"]
    assert out["merged"] == 1


def test_remap_topics_leaves_distinct(store, tmp_path):
    store.upsert_entity("topic-prayer", "prayer", "topic", "", "2026-01-01")
    store.upsert_entity("topic-prayer-meeting", "prayer meeting", "topic", "", "2026-01-01")
    consolidate.remap_topics(store, str(tmp_path))
    with store._connect() as db:
        n = db.execute("SELECT COUNT(*) FROM entities WHERE type='topic'").fetchone()[0]
    assert n == 2  # no synonym entry -> stay distinct


def test_reset_meeting_sources(store):
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_chunk("gmail-m1-body-0", "t", "h", {"message_id": "m1"})
    store.mark_enriched(["gmail-m1-body-0"])
    store.link_email_entity("m1", "board-12-may", role="about")
    out = consolidate.reset_meeting_sources(store)
    assert "board-12-may" in out["pre_ids"]
    assert out["chunks_reset"] == 1


def test_retire_meeting_duplicates(store):
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_entity("meeting-acme-board", "Board", "meeting", "Acme", "2026-05-12")
    store.link_email_entity("m1", "board-12-may", role="about")
    store.link_email_entity("m1", "meeting-acme-board", role="about")
    out = consolidate.retire_meeting_duplicates(store, ["board-12-may"])
    with store._connect() as db:
        remaining = db.execute("SELECT id FROM entities WHERE type='meeting'").fetchall()
    assert [r["id"] for r in remaining] == ["meeting-acme-board"]
    assert out["retired"] == 1


def _load_bin():
    path = pathlib.Path(__file__).resolve().parents[1] / "bin" / "consolidate.py"
    spec = importlib.util.spec_from_file_location("bin_consolidate", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_backup_db_creates_copy(tmp_path):
    db = tmp_path / "brain.db"; db.write_bytes(b"sqlitedata")
    mod = _load_bin()
    backup = mod._backup_db(db)
    assert backup.exists() and backup.read_bytes() == b"sqlitedata"
    assert backup != db
