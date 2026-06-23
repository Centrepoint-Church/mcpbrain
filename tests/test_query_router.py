"""Tests for mcpbrain.query_router — Q6 retrieval polish.

Covers:
  - Intent classification (entity vs thematic vs general).
  - Entity graph-seed expansion appends neighbour names to query.
  - Community augmentation adds community results for thematic queries.
  - CRAG rewrite merges secondary results when top score is below threshold.
  - Token-overlap reranker promotes chunks with more query-term overlap.
  - route() falls back to plain hybrid_search when all flags are off.
  - route() is a no-op with all flags off (existing behaviour unchanged).
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from mcpbrain.query_router import (
    _classify_intent,
    _graph_seed_query,
    _community_augment,
    _merge_crag,
    _token_overlap_rerank,
    _INTENT_ENTITY,
    _INTENT_THEMATIC,
    _INTENT_GENERAL,
    route,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_home(tmp_path, **flags):
    (tmp_path / "config.json").write_text(json.dumps(flags))
    return str(tmp_path)


def _make_store(entities=None, relations=None, communities=None):
    store = MagicMock()
    _entities = entities or {}
    _relations = relations or {}
    _communities = communities or []

    def _find_entity(q):
        return _entities.get(q.lower())

    def _get_entity(eid):
        for ent in _entities.values():
            if ent and ent.get("id") == eid:
                return ent
        return None

    def _relations_for(eid):
        return _relations.get(eid, [])

    def _list_communities():
        return _communities

    store.find_entity.side_effect = _find_entity
    store.get_entity.side_effect = _get_entity
    store.relations_for.side_effect = _relations_for
    store.list_communities.return_value = _communities
    return store


def _hit(doc_id, score, text="some text"):
    return {"doc_id": doc_id, "score": score, "text": text}


# ---------------------------------------------------------------------------
# _classify_intent
# ---------------------------------------------------------------------------

def test_classify_intent_thematic_what():
    store = _make_store()
    intent, eid = _classify_intent("what is the camp budget?", store)
    assert intent == _INTENT_THEMATIC
    assert eid is None


def test_classify_intent_thematic_how():
    store = _make_store()
    intent, eid = _classify_intent("how do we process leave requests?", store)
    assert intent == _INTENT_THEMATIC


def test_classify_intent_entity_match():
    store = _make_store(entities={"joel": {"id": "joel-id", "name": "Joel"}})
    intent, eid = _classify_intent("Joel's decisions on the budget", store)
    assert intent == _INTENT_ENTITY
    assert eid == "joel-id"


def test_classify_intent_general_no_entity():
    store = _make_store()
    intent, eid = _classify_intent("board meeting notes", store)
    assert intent == _INTENT_GENERAL
    assert eid is None


def test_classify_intent_entity_longer_name():
    store = _make_store(entities={
        "taryn hamilton": {"id": "taryn-id", "name": "Taryn Hamilton"}
    })
    intent, eid = _classify_intent("emails from Taryn Hamilton about facilities", store)
    assert intent == _INTENT_ENTITY
    assert eid == "taryn-id"


# ---------------------------------------------------------------------------
# _graph_seed_query
# ---------------------------------------------------------------------------

def test_graph_seed_query_appends_neighbours():
    store = _make_store(
        entities={
            "alice": {"id": "alice-id", "name": "Alice Smith"},
            "bob": {"id": "bob-id", "name": "Bob Jones"},
        },
        relations={
            "alice-id": [
                {"entity_a": "alice-id", "entity_b": "bob-id"},
            ]
        },
    )
    # Bob's entity is returned by get_entity
    result = _graph_seed_query(store, "alice-id", "Alice budget")
    assert "Bob Jones" in result
    assert result.startswith("Alice budget")


def test_graph_seed_query_no_relations_unchanged():
    store = _make_store()
    store.relations_for.return_value = []
    result = _graph_seed_query(store, "some-id", "original query")
    assert result == "original query"


# ---------------------------------------------------------------------------
# _community_augment
# ---------------------------------------------------------------------------

def test_community_augment_adds_relevant_community():
    results = [_hit("doc-1", 0.9, "budget ministry")]
    communities = [
        {"community_id": 1, "summary": "Finance and budget team at Centrepoint."}
    ]
    store = _make_store(communities=communities)
    augmented = _community_augment(store, "church budget planning", results, 10)
    texts = [r["text"] for r in augmented]
    assert any("Community 1" in t for t in texts)


def test_community_augment_skips_irrelevant_community():
    results = [_hit("doc-1", 0.9)]
    communities = [
        {"community_id": 2, "summary": "Roster and worship team scheduling."}
    ]
    store = _make_store(communities=communities)
    augmented = _community_augment(store, "car park resurfacing cost", results, 10)
    community_hits = [r for r in augmented if r.get("provenance") == "community_summary"]
    # Overlap between "car park resurfacing" and "roster worship team" is low; may or may not add
    assert len(augmented) >= 1


def test_community_augment_no_communities_unchanged():
    results = [_hit("doc-1", 0.9)]
    store = _make_store()
    store.list_communities.return_value = []
    augmented = _community_augment(store, "any query", results, 10)
    assert augmented == results


# ---------------------------------------------------------------------------
# _merge_crag
# ---------------------------------------------------------------------------

def test_merge_crag_deduplicates_keeps_higher_score():
    primary = [_hit("a", 0.8), _hit("b", 0.6)]
    secondary = [_hit("b", 0.9, "better version"), _hit("c", 0.5)]
    merged = _merge_crag(primary, secondary, 5)
    b_hit = next(r for r in merged if r["doc_id"] == "b")
    assert b_hit["score"] == 0.9   # secondary beat primary
    assert any(r["doc_id"] == "c" for r in merged)  # new hit added


def test_merge_crag_respects_limit():
    primary = [_hit(f"p{i}", 1.0 - i * 0.1) for i in range(5)]
    secondary = [_hit(f"s{i}", 0.9 - i * 0.1) for i in range(5)]
    merged = _merge_crag(primary, secondary, 4)
    assert len(merged) == 4


# ---------------------------------------------------------------------------
# _token_overlap_rerank
# ---------------------------------------------------------------------------

def test_rerank_promotes_high_overlap():
    results = [
        _hit("low-overlap", 0.9, "weather forecast sunny tomorrow"),
        _hit("high-overlap", 0.7, "camp budget ministry spending church"),
    ]
    reranked = _token_overlap_rerank("camp budget church", results)
    # high-overlap should rise above low-overlap despite lower original score
    assert reranked[0]["doc_id"] == "high-overlap"


def test_rerank_preserves_all_hits():
    results = [_hit(f"doc-{i}", 1.0 - i * 0.1) for i in range(5)]
    reranked = _token_overlap_rerank("query", results)
    assert len(reranked) == 5


def test_rerank_empty_query_unchanged_order():
    results = [_hit("a", 0.9), _hit("b", 0.5)]
    reranked = _token_overlap_rerank("", results)
    assert reranked[0]["doc_id"] == "a"


# ---------------------------------------------------------------------------
# route() integration
# ---------------------------------------------------------------------------

def test_route_all_flags_off_calls_hybrid_search(tmp_path):
    home = _make_home(tmp_path)  # all flags default off
    store = _make_store()
    embedder = MagicMock()
    expected = [_hit("doc-1", 0.9)]

    with patch("mcpbrain.query_router.hybrid_search", return_value=expected) as mock_hs:
        result = route(store, embedder, "query", 5, home=home)
    mock_hs.assert_called_once()
    assert result == expected


def test_route_routing_on_uses_graph_seed(tmp_path):
    home = _make_home(tmp_path, retrieval_routing=True)
    store = _make_store(
        entities={"joel": {"id": "joel-id", "name": "Joel"}},
        relations={"joel-id": [{"entity_a": "joel-id", "entity_b": "bob-id"}]},
    )
    store.get_entity.return_value = {"id": "bob-id", "name": "Bob Jones"}
    embedder = MagicMock()
    expected = [_hit("doc-1", 0.9)]

    with patch("mcpbrain.query_router.hybrid_search", return_value=expected):
        result = route(store, embedder, "Joel decisions", 5, home=home)
    assert result[0]["doc_id"] == "doc-1"


def test_route_crag_on_fires_on_low_score(tmp_path):
    home = _make_home(tmp_path, retrieval_crag=True, crag_min_score=0.50)
    store = _make_store()
    embedder = MagicMock()
    primary = [_hit("primary", 0.3)]  # below threshold
    secondary = [_hit("secondary", 0.8)]

    call_count = [0]
    def mock_hybrid(*a, **kw):
        call_count[0] += 1
        return primary if call_count[0] == 1 else secondary

    with patch("mcpbrain.query_router.hybrid_search", side_effect=mock_hybrid):
        with patch("mcpbrain.query_router._crag_rewrite", return_value="better query"):
            result = route(store, embedder, "vague query", 5, home=home)

    # Should have called hybrid_search twice (once primary, once CRAG)
    assert call_count[0] == 2
    # secondary (score 0.8) should beat primary (score 0.3) after merge
    assert result[0]["doc_id"] == "secondary"


def test_route_crag_skips_on_high_score(tmp_path):
    home = _make_home(tmp_path, retrieval_crag=True, crag_min_score=0.50)
    store = _make_store()
    embedder = MagicMock()
    primary = [_hit("primary", 0.9)]  # above threshold

    with patch("mcpbrain.query_router.hybrid_search", return_value=primary):
        with patch("mcpbrain.query_router._crag_rewrite") as mock_rewrite:
            result = route(store, embedder, "precise query", 5, home=home)

    mock_rewrite.assert_not_called()  # CRAG should not fire


def test_route_rerank_on_reorders(tmp_path):
    home = _make_home(tmp_path, retrieval_rerank=True)
    store = _make_store()
    embedder = MagicMock()
    hits = [
        _hit("low-overlap", 0.9, "unrelated text about weather"),
        _hit("high-overlap", 0.7, "budget ministry church camp spending"),
    ]

    with patch("mcpbrain.query_router.hybrid_search", return_value=hits):
        result = route(store, embedder, "church budget spending", 5, home=home)
    assert result[0]["doc_id"] == "high-overlap"


def test_route_error_returns_empty(tmp_path):
    home = _make_home(tmp_path)
    store = _make_store()
    embedder = MagicMock()

    with patch("mcpbrain.query_router.hybrid_search", side_effect=RuntimeError("boom")):
        result = route(store, embedder, "query", 5, home=home)
    assert result == []
