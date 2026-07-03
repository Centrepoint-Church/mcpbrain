"""One-shot migration logic (Boulder 3, Task 11) and the attended CLI (Task 12)."""

import importlib.util
import pathlib

import pytest

from mcpbrain import consolidate
from mcpbrain.graph_write import upsert_relation
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


def test_remap_topics_renormalizes_email_context(store, tmp_path):
    # Historical email_context.topics strings hold un-normalized tags — F-M4
    # requires remap_topics to renormalize them too, so the min-2-org gate
    # doesn't transiently mix old/new forms of the same topic.
    store.upsert_email_context("m1", subject="s", topics="budgets, prayer")
    out = consolidate.remap_topics(store, str(tmp_path))
    with store._connect() as db:
        row = db.execute(
            "SELECT topics FROM email_context WHERE message_id='m1'").fetchone()
    assert row["topics"] == "budget, prayer"
    assert out["rows_renormalized"] == 1


def test_reset_meeting_sources(store):
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_chunk("gmail-m1-body-0", "t", "h", {"message_id": "m1"})
    store.mark_enriched(["gmail-m1-body-0"])
    store.link_email_entity("m1", "board-12-may", role="about")
    out = consolidate.reset_meeting_sources(store)
    assert "board-12-may" in out["pre_ids"]
    assert out["chunks_reset"] == 1


def test_reset_meeting_sources_unclods_chunks(store):
    # A meeting-source chunk gated 'cold' by the Q1 salience gate would
    # otherwise be reset to enriched=0 but never re-queue, since
    # unenriched_chunks() excludes cold chunks. reset_meeting_sources must
    # clear enrich_state too, scoped to exactly the meeting-source doc_ids.
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_chunk("gmail-m1-body-0", "t", "h", {"message_id": "m1"})
    store.mark_enriched(["gmail-m1-body-0"])
    store.set_enrich_state(["gmail-m1-body-0"], "cold")
    store.link_email_entity("m1", "board-12-may", role="about")
    out = consolidate.reset_meeting_sources(store)
    with store._connect() as db:
        row = db.execute(
            "SELECT enriched, enrich_state FROM chunks WHERE doc_id='gmail-m1-body-0'"
        ).fetchone()
    assert row["enriched"] == 0
    assert row["enrich_state"] == ""
    assert out["uncold"] == 1


def test_reset_meeting_sources_recovers_calendar_evidence(store):
    # Calendar-sourced meetings: attended/instance_of/involved_in relations are
    # written via the generic structural pass with prov_doc_id empty (the
    # calendar-chunk enrichment path never threads doc_ids through), so
    # source_doc_id lands as '' even though evidence holds the bare Calendar
    # event id. meeting_source_doc_ids() must recover the chunk via the
    # 'cal-<event_id>' doc_id convention.
    store.upsert_entity("meeting-acme-standup", "Standup", "meeting", "Acme", "2026-05-12")
    store.upsert_chunk("cal-evt123", "t", "h", {"event_id": "evt123"})
    store.mark_enriched(["cal-evt123"])
    upsert_relation(store, "meeting-acme-standup", "involved_in", "topic-standup",
                     valid_from="2026-05-12", evidence="evt123", source_doc_id="")
    out = consolidate.reset_meeting_sources(store)
    assert out["chunks_reset"] == 1


def test_reset_meeting_sources_recovers_calendar_evidence_recurring_instance(store):
    # A recurring event's chunk doc_id carries a per-instance date suffix
    # ('cal-<event_id>_<instant>'), so the recovery match must be a prefix
    # match, not exact equality.
    store.upsert_entity("meeting-acme-standup", "Standup", "meeting", "Acme", "2026-05-12")
    store.upsert_chunk("cal-evt123_20260512T090000Z", "t", "h", {"event_id": "evt123"})
    store.mark_enriched(["cal-evt123_20260512T090000Z"])
    upsert_relation(store, "meeting-acme-standup", "involved_in", "topic-standup",
                     valid_from="2026-05-12", evidence="evt123", source_doc_id="")
    out = consolidate.reset_meeting_sources(store)
    assert out["chunks_reset"] == 1


def test_retire_meeting_duplicates(store):
    store.upsert_entity("board-12-may", "Board 12 May", "meeting", "Acme", "2026-05-12")
    store.upsert_entity("meeting-acme-board", "Board", "meeting", "Acme", "2026-05-12")
    # A GENUINE series carries an occurrence observation (M8) — without one it
    # no longer qualifies as a candidate series for meeting_series_for_old.
    store.append_occurrence("meeting-acme-board", "2026-05-12", "board mtg", "m1")
    store.link_email_entity("m1", "board-12-may", role="about")
    store.link_email_entity("m1", "meeting-acme-board", role="about")
    out = consolidate.retire_meeting_duplicates(store, ["board-12-may"])
    with store._connect() as db:
        remaining = db.execute("SELECT id FROM entities WHERE type='meeting'").fetchall()
    assert [r["id"] for r in remaining] == ["meeting-acme-board"]
    assert out["retired"] == 1


def test_retire_meeting_duplicates_legacy_bare_meeting_slug(store):
    # A legacy meeting entity whose slug happens to start with 'meeting-'
    # ("Meeting with Bob" -> 'meeting-with-bob') must still be retired into a
    # GENUINE re-extracted series — the old `startswith("meeting-")` guard
    # false-positived on exactly this shape (M8b).
    store.upsert_entity("meeting-with-bob", "Meeting with Bob", "meeting", "Acme", "2026-05-12")
    store.upsert_entity("meeting-acme-standup", "Standup", "meeting", "Acme", "2026-05-12")
    store.append_occurrence("meeting-acme-standup", "2026-05-12", "standup", "m1")
    store.link_email_entity("m1", "meeting-with-bob", role="about")
    store.link_email_entity("m1", "meeting-acme-standup", role="about")
    out = consolidate.retire_meeting_duplicates(store, ["meeting-with-bob"])
    with store._connect() as db:
        remaining = db.execute("SELECT id FROM entities WHERE type='meeting'").fetchall()
    assert [r["id"] for r in remaining] == ["meeting-acme-standup"]
    assert out["retired"] == 1


def test_retire_meeting_duplicates_skips_genuine_series_in_pre_ids(store):
    # A genuine series (has an occurrence) passed in pre_ids — e.g. on a
    # re-run — must be skipped entirely: not merged into anything, still
    # present, and not counted as retired or left.
    store.upsert_entity("meeting-acme-standup", "Standup", "meeting", "Acme", "2026-05-12")
    store.append_occurrence("meeting-acme-standup", "2026-05-12", "standup", "m1")
    out = consolidate.retire_meeting_duplicates(store, ["meeting-acme-standup"])
    with store._connect() as db:
        remaining = [r["id"] for r in db.execute(
            "SELECT id FROM entities WHERE type='meeting'").fetchall()]
    assert remaining == ["meeting-acme-standup"]
    assert out["retired"] == 0
    assert out["left"] == 0


def _load_bin():
    path = pathlib.Path(__file__).resolve().parents[1] / "bin" / "consolidate.py"
    spec = importlib.util.spec_from_file_location("bin_consolidate", path)
    mod = importlib.util.module_from_spec(spec); spec.loader.exec_module(mod)
    return mod


def test_backup_db_creates_copy(tmp_path):
    # _backup_db routes through backup.snapshot(), which runs a WAL checkpoint
    # then copies — so it needs a REAL SQLite store, not raw bytes. Build one,
    # seed a row, and assert the backup is a distinct file that is itself a valid
    # SQLite copy carrying that row (proves a real, restorable snapshot).
    db = tmp_path / "brain.db"
    s = Store(db, dim=4)
    s.init()
    s.upsert_entity("topic-budget", "budget", "topic", "", "2026-01-01")

    mod = _load_bin()
    backup = mod._backup_db(db)

    assert backup.exists()
    assert backup != db
    restored = Store(backup, dim=4)
    with restored._connect() as rdb:
        row = rdb.execute("SELECT name FROM entities WHERE id='topic-budget'").fetchone()
    assert row["name"] == "budget"
