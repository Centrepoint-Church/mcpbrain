"""End-to-end loop: FakeGoogleService -> real sync -> real prepare ->
hand-written enrich_inbox (stubbed Cowork extractor) -> real drain ->
real graph_write.apply -> graph + dashboard.

Only two things are faked: Google's API (FakeGoogleService) and the Claude
enrichment step (a hand-authored, contract-valid enrich_inbox file). Everything
between is the real code path. The non-empty guards make a no-op pipeline fail
loudly.
"""
import pytest

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
