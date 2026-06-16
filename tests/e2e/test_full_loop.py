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

from mcpbrain import prepare
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
