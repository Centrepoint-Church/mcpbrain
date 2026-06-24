from mcpbrain.store import Store
from mcpbrain.retrieval import hybrid_search, action_is_stale, annotate_action_freshness


class FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] if "budget" in t else [0, 1.0, 0, 0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0, 0, 0] if "budget" in text else [0, 1.0, 0, 0]


def _seed(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("d-budget", "the annual budget review", "h1", {})
    s.upsert_chunk("d-roster", "the volunteer roster", "h2", {})
    from mcpbrain.index import index_pending
    index_pending(s, FakeEmbedder())
    return s


def test_semantic_finds_paraphrase(tmp_path):
    s = _seed(tmp_path)
    ids = [r["doc_id"] for r in hybrid_search(s, FakeEmbedder(), "money planning", limit=2)]
    assert "d-budget" in ids


def test_keyword_finds_exact_term(tmp_path):
    s = _seed(tmp_path)
    ids = [r["doc_id"] for r in hybrid_search(s, FakeEmbedder(), "roster", limit=2)]
    assert "d-roster" in ids


def test_keyword_query_with_fts_special_chars_does_not_crash(tmp_path):
    """A query containing FTS5 operator chars (hyphens, colons, quotes) must not
    raise 'no such column' — it should be treated as literal search terms."""
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("d-hyphen", "VERIFY-CAP-001 probe token", "h1", {})
    s.upsert_chunk("d-roster", "the volunteer roster", "h2", {})
    from mcpbrain.index import index_pending
    index_pending(s, FakeEmbedder())
    # None of these should raise (previously crashed on the leading/embedded '-').
    for q in ["VERIFY-CAP-001", "a:b", 'has " quote', "trailing-", "-leading", "*", "("]:
        hybrid_search(s, FakeEmbedder(), q, limit=5)
    # And the exact hyphenated term must still retrieve its doc.
    ids = [r["doc_id"] for r in hybrid_search(s, FakeEmbedder(), "VERIFY-CAP-001", limit=5)]
    assert "d-hyphen" in ids


def test_hybrid_search_skips_expired_notes(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("note-budget", "the annual budget review", "h1",
                   {"source": "note", "expired": True})
    s.upsert_chunk("d-other", "the volunteer roster", "h2", {})
    from mcpbrain.index import index_pending
    index_pending(s, FakeEmbedder())
    ids = [r["doc_id"] for r in hybrid_search(s, FakeEmbedder(), "budget", limit=5)]
    assert "note-budget" not in ids   # expired note must not surface


# --- action freshness (Task 4.4) -----------------------------------------

# RFC2822 dates: msg-a is earlier, msg-b is later.
_DATE_EARLY = "Mon, 26 May 2026 09:00:00 +0800"
_DATE_LATE  = "Tue, 27 May 2026 14:00:00 +0800"


def _freshness_store(tmp_path):
    s = Store(tmp_path / "fresh.sqlite3", dim=4)
    s.init()
    return s


def test_action_is_stale_when_thread_has_resolution_reply(tmp_path):
    """Resolved-thread fixture: msg-a is the request, msg-b has 'done' and is newer."""
    s = _freshness_store(tmp_path)
    s.upsert_chunk("gmail-t1-a", "Can you send the campus budget?", "h1",
                   {"thread_id": "t1", "message_id": "msg-a",
                    "date": _DATE_EARLY, "source_type": "gmail"})
    s.upsert_chunk("gmail-t1-b", "Done, sent it through.", "h2",
                   {"thread_id": "t1", "message_id": "msg-b",
                    "date": _DATE_LATE, "source_type": "gmail"})
    s.add_unified_action(text="Send the campus budget", owner="Sam",
                         source_doc_id="gmail-t1-a", thread_id="t1")
    action = s.list_unified_actions()[0]

    assert action_is_stale(s, action) is True
    annotated = annotate_action_freshness(s, [action])
    assert annotated[0]["freshness"] == "stale"
    # original dict must not be mutated
    assert "freshness" not in action


def test_action_is_fresh_when_thread_has_no_resolution(tmp_path):
    """Open-thread fixture: only the source message, no reply."""
    s = _freshness_store(tmp_path)
    s.upsert_chunk("gmail-t2-a", "Can you send the campus budget?", "h1",
                   {"thread_id": "t2", "message_id": "msg-a",
                    "date": _DATE_EARLY, "source_type": "gmail"})
    s.add_unified_action(text="Send the campus budget", owner="Sam",
                         source_doc_id="gmail-t2-a", thread_id="t2")
    action = s.list_unified_actions()[0]

    assert action_is_stale(s, action) is False
    annotated = annotate_action_freshness(s, [action])
    assert annotated[0]["freshness"] == "fresh"


def test_action_is_fresh_when_resolution_only_in_source_message(tmp_path):
    """Source contains 'done' but no other message resolves it — source is skipped."""
    s = _freshness_store(tmp_path)
    s.upsert_chunk("gmail-t3-a", "We need to get this done please.", "h1",
                   {"thread_id": "t3", "message_id": "msg-a",
                    "date": _DATE_EARLY, "source_type": "gmail"})
    s.add_unified_action(text="Get this done", owner="Sam",
                         source_doc_id="gmail-t3-a", thread_id="t3")
    action = s.list_unified_actions()[0]

    assert action_is_stale(s, action) is False


def test_action_is_fresh_when_resolution_predates_request(tmp_path):
    """Reply contains 'done' but is OLDER than the source message — not stale."""
    s = _freshness_store(tmp_path)
    # msg-b is the older message (dates swapped vs normal fixture)
    s.upsert_chunk("gmail-t4-a", "Can you send the campus budget?", "h1",
                   {"thread_id": "t4", "message_id": "msg-a",
                    "date": _DATE_LATE, "source_type": "gmail"})
    s.upsert_chunk("gmail-t4-b", "All good, handled it.", "h2",
                   {"thread_id": "t4", "message_id": "msg-b",
                    "date": _DATE_EARLY, "source_type": "gmail"})
    s.add_unified_action(text="Send the campus budget", owner="Sam",
                         source_doc_id="gmail-t4-a", thread_id="t4")
    action = s.list_unified_actions()[0]

    # Resolution predates the request — should not be stale
    assert action_is_stale(s, action) is False


def test_action_is_stale_with_mixed_naive_and_aware_dates(tmp_path):
    """Regression: Gmail uses '-0000' (naive) on automated replies while real
    sends carry an offset like '+0800' (aware). Comparing one of each must not
    raise TypeError. Source +0800 09:00 = 01:00 UTC; reply -0000 05:00 = 05:00
    UTC, so the reply is genuinely later and the action is stale."""
    s = _freshness_store(tmp_path)
    s.upsert_chunk("gmail-t6-a", "Can you send the campus budget?", "h1",
                   {"thread_id": "t6", "message_id": "msg-a",
                    "date": "Mon, 26 May 2026 09:00:00 +0800", "source_type": "gmail"})
    s.upsert_chunk("gmail-t6-b", "Done, sent it through.", "h2",
                   {"thread_id": "t6", "message_id": "msg-b",
                    "date": "Mon, 26 May 2026 05:00:00 -0000", "source_type": "gmail"})
    s.add_unified_action(text="Send the campus budget", owner="Sam",
                         source_doc_id="gmail-t6-a", thread_id="t6")
    action = s.list_unified_actions()[0]

    # Must not raise (pre-fix: TypeError comparing naive vs aware datetimes).
    assert action_is_stale(s, action) is True


def test_action_is_fresh_with_forward_looking_done(tmp_path):
    """'I'll get it done next week' is forward-looking, not a resolution."""
    s = _freshness_store(tmp_path)
    s.upsert_chunk("gmail-t7-a", "Can you send the campus budget?", "h1",
                   {"thread_id": "t7", "message_id": "msg-a",
                    "date": _DATE_EARLY, "source_type": "gmail"})
    s.upsert_chunk("gmail-t7-b", "I'll get it done next week.", "h2",
                   {"thread_id": "t7", "message_id": "msg-b",
                    "date": _DATE_LATE, "source_type": "gmail"})
    s.add_unified_action(text="Send the campus budget", owner="Sam",
                         source_doc_id="gmail-t7-a", thread_id="t7")
    action = s.list_unified_actions()[0]

    assert action_is_stale(s, action) is False


def test_action_is_fresh_with_well_done(tmp_path):
    """'well done everyone' praises, it doesn't resolve the request."""
    s = _freshness_store(tmp_path)
    s.upsert_chunk("gmail-t8-a", "Can you send the campus budget?", "h1",
                   {"thread_id": "t8", "message_id": "msg-a",
                    "date": _DATE_EARLY, "source_type": "gmail"})
    s.upsert_chunk("gmail-t8-b", "Well done everyone on the launch!", "h2",
                   {"thread_id": "t8", "message_id": "msg-b",
                    "date": _DATE_LATE, "source_type": "gmail"})
    s.add_unified_action(text="Send the campus budget", owner="Sam",
                         source_doc_id="gmail-t8-a", thread_id="t8")
    action = s.list_unified_actions()[0]

    assert action_is_stale(s, action) is False


def test_action_is_fresh_when_no_thread_id(tmp_path):
    """No thread_id: cannot inspect thread, so default to fresh."""
    s = _freshness_store(tmp_path)
    s.add_unified_action(text="Some orphan action", owner="Sam",
                         source_doc_id="", thread_id="")
    action = s.list_unified_actions()[0]

    assert action_is_stale(s, action) is False
    annotated = annotate_action_freshness(s, [action])
    assert annotated[0]["freshness"] == "fresh"


def test_hybrid_search_results_carry_normalised_score(tmp_path):
    s = _seed(tmp_path)
    results = hybrid_search(s, FakeEmbedder(), "budget", limit=2)
    assert results, "expected at least one hit"
    # Every result carries a float score in (0, 1].
    for r in results:
        assert "score" in r
        assert isinstance(r["score"], float)
        assert 0.0 < r["score"] <= 1.0
    # Normalisation: the top result's score is exactly 1.0.
    assert results[0]["score"] == 1.0
    # Scores are monotonically non-increasing (results stay rank-ordered).
    scores = [r["score"] for r in results]
    assert scores == sorted(scores, reverse=True)


def test_hybrid_search_score_is_stable_when_single_hit(tmp_path):
    """A single-hit result set must not divide-by-zero; its score is 1.0."""
    from mcpbrain.index import index_pending
    s = Store(tmp_path / "one.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("only", "the annual budget review", "h1", {})
    index_pending(s, FakeEmbedder())
    results = hybrid_search(s, FakeEmbedder(), "budget", limit=5)
    assert results[0]["score"] == 1.0


def test_rrf_weighting_is_tunable(tmp_path):
    """vec_weight / kw_weight scale each ranker's RRF contribution."""
    from mcpbrain.retrieval import _rrf
    sem = ["a", "b"]
    kw = ["b", "a"]
    base = _rrf([sem, kw])
    weighted = _rrf([sem, kw], vec_weight=2.0, kw_weight=0.0)
    # With kw zeroed, ordering follows the semantic ranking only.
    assert weighted["a"] > weighted["b"]
    # Base (equal weights) ties a and b (each appears once at rank 0 and once at rank 1).
    assert base["a"] == base["b"]


# --- Q6 contextual retrieval: prefix wiring + rollback flag --------------------

class _RecordingEmbedder:
    """Captures the exact passage texts handed to embed_passages."""
    dim = 4

    def __init__(self):
        self.seen = []

    def embed_passages(self, texts):
        self.seen.extend(texts)
        return [[1.0, 0, 0, 0] for _ in texts]

    def embed_query(self, text):
        return [1.0, 0, 0, 0]


def test_index_pending_prepends_contextual_prefix_by_default(tmp_path):
    """Q6 contextual retrieval is ON by default — index_pending prepends the
    provenance prefix to the passage before embedding (validated +recall/+MRR)."""
    from mcpbrain.index import index_pending
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    s.upsert_chunk("gmail-x-body-0", "the quarterly numbers", "h1",
                   {"source_type": "gmail", "sender": "alice@x.com",
                    "subject": "Q3 budget", "date": "2026-06-01"})
    emb = _RecordingEmbedder()
    index_pending(s, emb, home=str(tmp_path))   # no config.json → default True
    assert emb.seen and emb.seen[0].startswith("[Context: Email from alice@x.com")
    assert "the quarterly numbers" in emb.seen[0]


def test_index_pending_respects_disable_flag(tmp_path):
    """Setting contextual_retrieval=false embeds the raw text (rollback switch)."""
    import json
    from mcpbrain.index import index_pending
    (tmp_path / "config.json").write_text(json.dumps({"contextual_retrieval": False}))
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    s.upsert_chunk("gmail-x-body-0", "the quarterly numbers", "h1",
                   {"source_type": "gmail", "sender": "alice@x.com", "subject": "Q3"})
    emb = _RecordingEmbedder()
    index_pending(s, emb, home=str(tmp_path))
    assert emb.seen == ["the quarterly numbers"]   # no prefix when disabled
