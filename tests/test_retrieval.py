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


def test_hybrid_search_returns_one_hit_per_distinct_content(tmp_path, monkeypatch):
    """38,164 redundant copies survive the content-free purge: genuine duplicate
    FILES (the asset register exists three times in Drive). Deleting two of the
    three is the wrong fix — doc_ids are positional and cited as graph
    provenance, so it would make that file unfindable and orphan its rows. The
    real harm is recall crowding, and this is where crowding is fixed."""
    class _Emb:
        dim = 4

        def embed_passages(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    emb = _Emb()
    body = "The fixed asset register for the 2023 financial year."
    for n, fid in enumerate(("a", "b", "c")):
        store.upsert_chunk(f"gdrive-{fid}-0", body, "same-hash",
                           {"source_type": "gdrive", "file_id": fid,
                            "file_name": f"Asset Register{'' if n == 0 else f' ({n})'}.xlsx"})
        store.embed_doc(f"gdrive-{fid}-0", emb, home=str(tmp_path))
    store.upsert_chunk("gdrive-z-0", "Unrelated minutes of the board meeting.",
                       "other-hash", {"source_type": "gdrive", "file_id": "z"})
    store.embed_doc("gdrive-z-0", emb, home=str(tmp_path))

    hits = hybrid_search(store, emb, "asset register", limit=10)

    hashes = [h["content_hash"] for h in hits if h.get("content_hash")]
    assert len(hashes) == len(set(hashes)), (
        f"duplicate content crowded the result set: {hashes}"
    )
    assert sum(1 for h in hits if h["doc_id"].startswith("gdrive-") and
               h["doc_id"] != "gdrive-z-0") == 1


def test_dedup_keeps_the_best_ranked_representative(tmp_path, monkeypatch):
    """Which copy survives matters: dropping the top-ranked one would lower the
    result's quality while claiming to improve it."""
    from mcpbrain import retrieval

    hits = [{"doc_id": "d1", "content_hash": "h", "score": 0.9},
            {"doc_id": "d2", "content_hash": "h", "score": 0.5},
            {"doc_id": "d3", "content_hash": "other", "score": 0.7}]

    out = retrieval._dedupe_by_content(hits)

    assert [h["doc_id"] for h in out] == ["d1", "d3"]


def test_dedup_passes_through_hits_with_no_content_hash(tmp_path):
    """Not every producer sets it; a missing hash must never collapse rows."""
    from mcpbrain import retrieval

    hits = [{"doc_id": "d1", "score": 0.9}, {"doc_id": "d2", "score": 0.5}]

    assert len(retrieval._dedupe_by_content(hits)) == 2


def test_hybrid_search_collapses_a_multi_chunk_duplicate_file(tmp_path):
    """A genuine duplicate FILE spanning 2+ chunks per copy must still collapse
    to one surviving copy, not just single-chunk files. A per-chunk sibling
    COUNT (an earlier, rejected approach) is > 1 for EVERY chunk of a
    multi-chunk file, which would exempt the whole file from ever collapsing
    and leave it just as crowded as before; comparing the document's WHOLE
    hash SET is what correctly still collapses this case."""
    class _Emb:
        dim = 4

        def embed_passages(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    emb = _Emb()
    cover = "Asset register cover page for the 2023 financial year."
    body = "Assets: chairs, tables, projector, sound desk."
    for fid in ("p", "q"):
        store.upsert_chunk(f"gdrive-{fid}-0", cover, "cover-hash",
                           {"source_type": "gdrive", "file_id": fid})
        store.upsert_chunk(f"gdrive-{fid}-1", body, "body-hash",
                           {"source_type": "gdrive", "file_id": fid})
        store.embed_doc(f"gdrive-{fid}-0", emb, home=str(tmp_path))
        store.embed_doc(f"gdrive-{fid}-1", emb, home=str(tmp_path))
    store.upsert_chunk("gdrive-z-0", "Unrelated minutes of the board meeting.",
                       "other-hash", {"source_type": "gdrive", "file_id": "z"})
    store.embed_doc("gdrive-z-0", emb, home=str(tmp_path))

    hits = hybrid_search(store, emb, "asset register", limit=10)

    survivors = [h for h in hits if h["doc_id"].startswith(("gdrive-p-", "gdrive-q-"))]
    roots = {h["doc_id"].rsplit("-", 1)[0] for h in survivors}
    assert len(roots) == 1, f"both duplicate copies survived uncollapsed: {survivors}"
    assert {h["content_hash"] for h in survivors} == {"cover-hash", "body-hash"}, (
        "the surviving copy must keep BOTH of its own chunks, not just the one "
        "that happened to collide first"
    )
    assert any(h["doc_id"] == "gdrive-z-0" for h in hits), "unrelated content must survive untouched"


def test_hybrid_search_does_not_collapse_a_shared_boilerplate_chunk(tmp_path):
    """Two DIFFERENT real documents that merely share one templated/boilerplate
    chunk (e.g. a common board-charter letterhead) must NOT collapse into one
    — only a genuine WHOLE-document duplicate should. Comparing just the one
    shared chunk's content_hash can't tell these apart from a true duplicate
    file; comparing each document's full chunk-hash SET can (they differ,
    since the rest of each document's own content is unique per document)."""
    class _Emb:
        dim = 4

        def embed_passages(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    emb = _Emb()
    header = "Board Charter Template - Harvestnet Church Plant Policy Suite."
    for fid, body in (("alpha", "Governance structure for the Alpha church plant."),
                      ("beta", "Governance structure for the Beta church plant.")):
        store.upsert_chunk(f"gdrive-{fid}-0", header, "shared-header-hash",
                           {"source_type": "gdrive", "file_id": fid})
        store.upsert_chunk(f"gdrive-{fid}-1", body, f"{fid}-body-hash",
                           {"source_type": "gdrive", "file_id": fid})
        store.embed_doc(f"gdrive-{fid}-0", emb, home=str(tmp_path))
        store.embed_doc(f"gdrive-{fid}-1", emb, home=str(tmp_path))

    hits = hybrid_search(store, emb, "board charter template", limit=10)

    roots_present = {h["doc_id"].rsplit("-", 1)[0] for h in hits}
    assert {"gdrive-alpha", "gdrive-beta"} <= roots_present, (
        f"a genuinely different document was wrongly collapsed: {hits}"
    )


def test_dedup_keeps_content_the_surviving_copy_does_not_contribute(tmp_path):
    """Cluster membership is computed from the WHOLE database, but "what the
    surviving primary root contributes" is limited to the CANDIDATE POOL being
    deduped. When the pool holds a cluster-mate's chunk whose content_hash the
    primary root does NOT contribute to the pool — because that chunk was
    filtered out (metadata.expired here; exclude_cold cold-marking and the
    limit*2 retrieval boundary do the same thing) — dropping every non-primary
    chunk in the cluster makes that content vanish from the results entirely.
    That is content loss, not duplicate removal."""
    class _Emb:
        dim = 4

        def embed_passages(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    emb = _Emb()
    cover = "Asset register cover page for the 2023 financial year."
    body = "Assets: chairs, tables, projector, sound desk."
    # Two whole-document duplicates (identical hash SETS, so they cluster), with
    # `p` deterministically the primary: byte-identical content offers no
    # relevance signal, so the tie-break is recency and p is the newer copy.
    for fid, modified in (("p", "2026-07-01T00:00:00Z"), ("q", "2020-01-01T00:00:00Z")):
        store.upsert_chunk(f"gdrive-{fid}-0", cover, "cover-hash",
                           {"source_type": "gdrive", "file_id": fid,
                            "modified": modified})
        store.upsert_chunk(f"gdrive-{fid}-1", body, "body-hash",
                           {"source_type": "gdrive", "file_id": fid,
                            "modified": modified,
                            # The primary copy's body chunk is expired, so it is
                            # filtered out of the pool before dedup runs — the
                            # whole point: the cluster still knows about it (the
                            # hash set is read from the DB), the pool does not.
                            **({"expired": True} if fid == "p" else {})})
        store.embed_doc(f"gdrive-{fid}-0", emb, home=str(tmp_path))
        store.embed_doc(f"gdrive-{fid}-1", emb, home=str(tmp_path))

    hits = hybrid_search(store, emb, "asset register", limit=10)

    hashes = [h["content_hash"] for h in hits]
    assert "body-hash" in hashes, (
        "the duplicate-document dedup dropped content NOTHING in the pool "
        f"duplicates: {[h['doc_id'] for h in hits]}"
    )
    assert hits[0]["doc_id"] == "gdrive-p-0", "the primary copy still wins its hash"
    # Still deduped: exactly one hit per distinct content, not both copies.
    assert sorted(hashes) == ["body-hash", "cover-hash"], hashes
    assert [h["doc_id"] for h in hits if h["content_hash"] == "body-hash"] == \
        ["gdrive-q-1"], "the only unexpired copy of that content must survive"


def test_cluster_key_prefers_thread_id_then_file_id_then_calendar_event(tmp_path):
    from mcpbrain import retrieval

    assert retrieval._cluster_key({"metadata": {"thread_id": "t1", "file_id": "f1"}}) == "t1"
    assert retrieval._cluster_key({"metadata": {"file_id": "f1"}}) == "f1"
    assert retrieval._cluster_key({"metadata": {"event_id": "e1"}}) == "cal-e1"
    assert retrieval._cluster_key({"metadata": {}}) is None


def test_cluster_key_normalises_legacy_gdrive_prefixed_thread_id(tmp_path):
    """One live digest had its `thread_id` stamped as a raw chunk's full doc_id
    shape (`gdrive-<file_id>-<n>`) instead of the bare file_id current writers
    use — normalise both to the same key so it still clusters correctly."""
    from mcpbrain import retrieval

    a = retrieval._cluster_key({"metadata": {"thread_id": "gdrive-abc123-0"}})
    b = retrieval._cluster_key({"metadata": {"file_id": "abc123"}})
    assert a == b == "abc123"


def test_dedup_by_cluster_drops_a_digest_when_its_own_raw_sibling_is_present(tmp_path):
    """A digest (`enriched-<thread_id>`) is a synthesized summary of its own
    thread's raw chunks — pure derived output. When both are in the pool it is
    redundant, and since a digest can never itself match a gold/expected
    source document, letting it occupy a slot ahead of its own raw sibling
    only pushes the real answer down. The raw chunk must survive."""
    from mcpbrain import retrieval

    hits = [
        {"doc_id": "enriched-t1", "metadata": {"thread_id": "t1"}, "score": 0.9},
        {"doc_id": "gdrive-t1-0", "metadata": {"thread_id": "t1"}, "score": 0.5},
        {"doc_id": "gdrive-z-0", "metadata": {"thread_id": "z"}, "score": 0.7},
    ]

    out = retrieval._dedupe_by_cluster(hits)

    assert [h["doc_id"] for h in out] == ["gdrive-t1-0", "gdrive-z-0"]


def test_dedup_by_cluster_keeps_a_digest_with_no_raw_sibling_in_the_pool(tmp_path):
    """If a thread's raw chunks aren't in THIS pool (filtered out, beyond the
    retrieval boundary, etc.) its digest is the only representation present —
    dropping it would be content loss, not duplicate removal."""
    from mcpbrain import retrieval

    hits = [
        {"doc_id": "enriched-t1", "metadata": {"thread_id": "t1"}, "score": 0.9},
        {"doc_id": "gdrive-z-0", "metadata": {"thread_id": "z"}, "score": 0.7},
    ]

    out = retrieval._dedupe_by_cluster(hits)

    assert [h["doc_id"] for h in out] == ["enriched-t1", "gdrive-z-0"]


def test_dedup_by_cluster_does_not_collapse_two_raw_chunks_of_the_same_thread(tmp_path):
    """Only a digest-vs-raw pair is redundant. Two different raw messages in
    the same thread carry genuinely distinct content and must both survive."""
    from mcpbrain import retrieval

    hits = [
        {"doc_id": "gdrive-t1-0", "metadata": {"thread_id": "t1"}, "score": 0.9},
        {"doc_id": "gdrive-t1-1", "metadata": {"thread_id": "t1"}, "score": 0.5},
    ]

    assert retrieval._dedupe_by_cluster(hits) == hits


def test_hybrid_search_does_not_let_a_digest_crowd_out_its_own_thread(tmp_path):
    """Integration-level: a thread's digest and its own raw source chunk both
    ranking near the top must not both survive — the raw chunk (the one that
    can actually match an expected/gold document) must be the one kept."""
    class _Emb:
        dim = 4

        def embed_passages(self, texts):
            return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

        def embed_query(self, text):
            return [1.0, 0.0, 0.0, 0.0]

    store = Store(tmp_path / "b.sqlite3", dim=4)
    store.init()
    emb = _Emb()
    store.upsert_chunk("gdrive-t1-0", "Fixed asset register review meeting notes.",
                       "raw-hash", {"source_type": "gdrive", "file_id": "t1",
                                    "thread_id": "t1"})
    store.upsert_chunk("enriched-t1", "Fixed asset register review meeting notes summary.",
                       "digest-hash", {"source_type": "gdrive_enriched_v2",
                                       "thread_id": "t1"})
    store.upsert_chunk("gdrive-z-0", "Unrelated minutes of the board meeting.",
                       "other-hash", {"source_type": "gdrive", "file_id": "z"})
    store.embed_doc("gdrive-t1-0", emb, home=str(tmp_path))
    store.embed_doc("enriched-t1", emb, home=str(tmp_path))
    store.embed_doc("gdrive-z-0", emb, home=str(tmp_path))

    hits = hybrid_search(store, emb, "asset register", limit=10)

    doc_ids = [h["doc_id"] for h in hits]
    assert "gdrive-t1-0" in doc_ids, f"raw source chunk was crowded out: {doc_ids}"
    assert "enriched-t1" not in doc_ids, (
        f"digest survived alongside its own raw sibling: {doc_ids}"
    )
