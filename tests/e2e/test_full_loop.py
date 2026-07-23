"""End-to-end loop: FakeGoogleService -> real sync -> real prepare_units ->
hand-written enrich_inbox (stubbed Cowork extractor) -> real drain ->
real graph_write.apply -> graph + dashboard.

Only two things are faked: Google's API (FakeGoogleService) and the Claude
enrichment step (a hand-authored, contract-valid enrich_inbox file). Everything
between is the real code path (prepare_units — the work-queue producer real
daemon cycles call; prepare.prepare()/pending.json were deleted, so the hand
extraction is built from the immutable work-unit files under
enrich_queue/units/ instead of a single pending.json). The non-empty guards
make a no-op pipeline fail loudly.
"""
import json

import pytest

from mcpbrain import dashboard, drain, graph_write, prepare
from mcpbrain.contract import validate_batch_file
from mcpbrain.sync.calendar import backfill_calendar_window
from mcpbrain.sync.drive import backfill_drive
from mcpbrain.sync.gmail import backfill_gmail

pytestmark = pytest.mark.e2e


def test_gmail_backfill_lands_chunks(e2e_store, fake_google):
    n = backfill_gmail(fake_google, e2e_store, after="2026/01/01")
    assert n >= 2, "fixture threads should produce chunks"

    # Index all chunks into FTS with dummy zero-vectors (no embedder needed for
    # this assertion; we only care that the text is searchable via FTS).
    for chunk in e2e_store.unembedded_chunks():
        e2e_store.write_embedding(chunk["rowid"], [0.0, 0.0, 0.0, 0.0])

    # known chunk is searchable by keyword (FTS, no embedder needed).
    hits = e2e_store.fts_search("Hall B", 5)
    assert hits, "a known fixture phrase must be findable"


def _read_unit_threads(home):
    """Collect every thread entry across enrich_queue/units/*.json (kind=="thread")."""
    threads = []
    units_dir = home / "enrich_queue" / "units"
    for u in units_dir.glob("*.json") if units_dir.exists() else []:
        d = json.loads(u.read_text())
        if d["kind"] == "thread":
            threads.extend(d["threads"])
    return threads


def test_prepare_units_spools_work_units(e2e_store, fake_google, e2e_home):
    backfill_gmail(fake_google, e2e_store, after="2026/01/01")
    summary = prepare.prepare_units(e2e_store, thread_cap=20, char_budget=24000,
                                    resolution_due=False)
    assert summary["threads"] >= 2, "non-noise fixture threads must spool"
    tids = {t["thread_id"] for t in _read_unit_threads(e2e_home)}
    assert tids, "unit files must list the synced threads"
    assert not (e2e_home / "enrich_queue" / "pending.json").exists()


def _hand_extraction(thread):
    """Build a contract-valid extraction from a pending.json thread entry."""
    msgs = thread["messages"]
    return {
        "thread_id": thread["thread_id"],
        "org": "Acme",
        "content_type": "request",
        "summary": "Joel asks Sam to confirm Hall B availability.",
        "entities": [
            {"name": "Joel Chelliah", "type": "person", "org": "Acme", "role": "Pastor"},
            {"name": "Acme Corp", "type": "org", "org": "Acme", "role": ""},
        ],
        "topics": ["facilities"],
        "actions": [{"description": "Confirm Hall B is free for Wednesday college",
                     "owner": ""}],
        "relations": [{"source_name": "Joel Chelliah", "type": "works_at",
                       "target_name": "Acme Corp"}],
        "messages": [{"message_id": m["message_id"], "sender": m.get("sender", ""),
                      "date": m["date"], "subject": m.get("subject", "")}
                     for m in msgs],
        "resolved_action_ids": [],
    }


def _run_full_loop(store, google, home):
    """Run sync -> prepare_units -> drain once. Returns (batch dict, drain summary).

    Home-consistency note: `prepare_units` resolves its home from MCPBRAIN_HOME
    (config.app_dir()) while `drain` is given home=str(home) explicitly. The
    e2e_home fixture sets MCPBRAIN_HOME to that same path, so the work units
    prepare_units writes are exactly the ones this hand-written inbox batch is
    built from. If the two ever diverged (the "home split-brain" class of bug),
    drain would find an empty inbox and summary["applied"] would be 0 — which
    the assertions below would catch.
    """
    backfill_gmail(google, store, after="2026/01/01")
    prepare.prepare_units(store, thread_cap=20, char_budget=24000, resolution_due=False)
    threads = _read_unit_threads(home)
    batch_id = "e2e-hand-batch"
    batch = {"batch_id": batch_id,
             "extractions": [_hand_extraction(t) for t in threads],
             "merge_answers": []}
    (home / "enrich_inbox" / f"{batch_id}.json").write_text(json.dumps(batch))
    summary = drain.drain(store, home=str(home), apply=graph_write.apply)
    return batch, summary


def test_full_loop_grows_graph_and_dashboard(e2e_store, fake_google, e2e_home):
    batch, summary = _run_full_loop(e2e_store, fake_google, e2e_home)

    # validate the hand-written batch satisfied the contract
    errors = validate_batch_file(batch)
    assert errors == [], f"hand-written batch must satisfy the contract: {errors}"

    assert summary["applied"] >= 2

    ents = e2e_store.list_entities()
    rels = e2e_store.list_relations()
    assert ents, "drain must have written entities"
    assert rels, "drain must have written relations"
    assert any(e["type"] == "person" for e in ents)
    assert any(e["type"] == "org" for e in ents)
    assert any("works_at" in str(r) for r in rels)


def test_dashboard_and_search_after_loop(e2e_store, fake_google, e2e_home):
    _run_full_loop(e2e_store, fake_google, e2e_home)  # batch, summary discarded

    # Index all chunks into FTS with dummy zero-vectors so FTS is populated.
    for chunk in e2e_store.unembedded_chunks():
        e2e_store.write_embedding(chunk["rowid"], [0.0, 0.0, 0.0, 0.0])

    payload = dashboard.assemble(e2e_store, str(e2e_home))
    actions = payload["actions"]
    total = sum(len(actions[b]) for b in ("overdue", "due_today", "upcoming", "blocked"))
    assert total >= 1, "the seeded action must surface in the dashboard"

    hits = e2e_store.fts_search("Hall B", 5)
    assert hits, "a known chunk must be findable after the full loop"


def test_drive_backfill_lands_doc_chunks_across_pages(e2e_store, fake_google):
    # backfill_drive must page through DRIVE_P2 to see doc-2; both docs index.
    n = backfill_drive(fake_google, e2e_store, modified_after="2026-01-01T00:00:00Z")
    assert n == 2, "both Drive docs (across two pages) must be indexed"

    for chunk in e2e_store.unembedded_chunks():
        e2e_store.write_embedding(chunk["rowid"], [0.0, 0.0, 0.0, 0.0])

    # Exported Google-Doc text (doc-1) and a text/plain file on page 2 (doc-2)
    # are both searchable — proves the export AND get_media paths + pagination.
    assert e2e_store.fts_search("reserved for college", 5), "exported Doc text must be indexed"
    assert e2e_store.fts_search("bus departs", 5), "page-2 text/plain file must be indexed"


def test_calendar_backfill_creates_attendee_graph(e2e_store, fake_google, e2e_home):
    # Owner is sam@acme.org (e2e_home). The event has Sam (self), Joel (real
    # attendee), and a room resource. Only Joel should become a person entity.
    n = backfill_calendar_window(
        fake_google, e2e_store,
        time_min="2026-06-01T00:00:00Z", time_max="2026-06-30T00:00:00Z")
    assert n >= 1, "the confirmed event must produce a chunk"

    ents = e2e_store.list_entities()
    names = {e["name"] for e in ents}
    assert "Joel Chelliah" in names, "a non-owner attendee must become a person entity"
    assert "Sam Admin" not in names, "the owner must be excluded"
    assert "Hall B" not in names, "room resources must be excluded"

    rels = e2e_store.list_relations()
    assert any("attended" in str(r) for r in rels), "an 'attended' relation must be written"
