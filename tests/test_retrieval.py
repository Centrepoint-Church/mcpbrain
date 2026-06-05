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
    s.add_action("Send the campus budget", owner="Josh",
                 source_doc_id="gmail-t1-a", thread_id="t1")
    action = s.list_actions()[0]

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
    s.add_action("Send the campus budget", owner="Josh",
                 source_doc_id="gmail-t2-a", thread_id="t2")
    action = s.list_actions()[0]

    assert action_is_stale(s, action) is False
    annotated = annotate_action_freshness(s, [action])
    assert annotated[0]["freshness"] == "fresh"


def test_action_is_fresh_when_resolution_only_in_source_message(tmp_path):
    """Source contains 'done' but no other message resolves it — source is skipped."""
    s = _freshness_store(tmp_path)
    s.upsert_chunk("gmail-t3-a", "We need to get this done please.", "h1",
                   {"thread_id": "t3", "message_id": "msg-a",
                    "date": _DATE_EARLY, "source_type": "gmail"})
    s.add_action("Get this done", owner="Josh",
                 source_doc_id="gmail-t3-a", thread_id="t3")
    action = s.list_actions()[0]

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
    s.add_action("Send the campus budget", owner="Josh",
                 source_doc_id="gmail-t4-a", thread_id="t4")
    action = s.list_actions()[0]

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
    s.add_action("Send the campus budget", owner="Josh",
                 source_doc_id="gmail-t6-a", thread_id="t6")
    action = s.list_actions()[0]

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
    s.add_action("Send the campus budget", owner="Josh",
                 source_doc_id="gmail-t7-a", thread_id="t7")
    action = s.list_actions()[0]

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
    s.add_action("Send the campus budget", owner="Josh",
                 source_doc_id="gmail-t8-a", thread_id="t8")
    action = s.list_actions()[0]

    assert action_is_stale(s, action) is False


def test_action_is_fresh_when_no_thread_id(tmp_path):
    """No thread_id: cannot inspect thread, so default to fresh."""
    s = _freshness_store(tmp_path)
    s.add_action("Some orphan action", owner="Josh",
                 source_doc_id="", thread_id="")
    action = s.list_actions()[0]

    assert action_is_stale(s, action) is False
    annotated = annotate_action_freshness(s, [action])
    assert annotated[0]["freshness"] == "fresh"
