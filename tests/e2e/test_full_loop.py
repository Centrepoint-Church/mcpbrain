"""End-to-end loop: FakeGoogleService -> real sync -> real prepare ->
hand-written enrich_inbox (stubbed Cowork extractor) -> real drain ->
real graph_write.apply -> graph + dashboard.

Only two things are faked: Google's API (FakeGoogleService) and the Claude
enrichment step (a hand-authored, contract-valid enrich_inbox file). Everything
between is the real code path. The non-empty guards make a no-op pipeline fail
loudly.
"""
import json

import pytest

from mcpbrain import drain, graph_write, prepare
from mcpbrain.contract import validate_batch_file
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


def test_prepare_spools_pending(e2e_store, fake_google, e2e_home):
    backfill_gmail(fake_google, e2e_store, after="2026/01/01")
    summary = prepare.prepare(e2e_store, thread_cap=20, char_budget=24000,
                              resolution_due=False)
    assert summary["threads"] >= 2, "non-noise fixture threads must spool"
    pending = json.loads((e2e_home / "enrich_queue" / "pending.json").read_text())
    tids = {t["thread_id"] for t in pending["threads"]}
    assert tids, "pending.json must list the synced threads"


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


def test_full_loop_grows_graph_and_dashboard(e2e_store, fake_google, e2e_home):
    backfill_gmail(fake_google, e2e_store, after="2026/01/01")
    prepare.prepare(e2e_store, thread_cap=20, char_budget=24000, resolution_due=False)
    pending = json.loads((e2e_home / "enrich_queue" / "pending.json").read_text())

    batch = {"batch_id": pending["batch_id"],
             "extractions": [_hand_extraction(t) for t in pending["threads"]],
             "merge_answers": []}
    # validate against the real contract before drain consumes it
    errors = validate_batch_file(batch)
    assert errors == [], f"hand-written batch must satisfy the contract: {errors}"
    (e2e_home / "enrich_inbox" / f"{batch['batch_id']}.json").write_text(json.dumps(batch))

    summary = drain.drain(e2e_store, home=str(e2e_home), apply=graph_write.apply)
    assert summary["applied"] >= 2

    ents = e2e_store.list_entities()
    rels = e2e_store.list_relations()
    assert ents, "drain must have written entities"
    assert rels, "drain must have written relations"
    assert any(e["type"] == "person" for e in ents)
    assert any(e["type"] == "org" for e in ents)
    assert any("works_at" in str(r) for r in rels)
