"""Tests for mcpbrain.sufficiency — S1 grounding/sufficiency gate.

Covers:
  - Gate is a no-op when flag is off (fast path, no subprocess).
  - Gate returns all hits unchanged when LLM is unavailable (fail-open).
  - Gate filters IRRELEVANT hits from a mocked LLM response.
  - Gate keeps RELEVANT hits through.
  - Gate never returns an empty list (fail-open when all are marked irrelevant).
  - High-similarity-but-irrelevant test: a chunk that matches topically but
    doesn't contain an answer to the query is filtered out while a genuinely
    relevant chunk is kept.
"""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from mcpbrain.sufficiency import filter_by_sufficiency, _parse_result, _build_prompt


# ---------------------------------------------------------------------------
# _parse_result unit tests
# ---------------------------------------------------------------------------

def test_parse_result_all_relevant():
    raw = json.dumps({"results": [{"idx": 1, "relevant": True}, {"idx": 2, "relevant": True}]})
    assert _parse_result(raw, 2) == [True, True]


def test_parse_result_mixed():
    raw = json.dumps({"results": [{"idx": 1, "relevant": True}, {"idx": 2, "relevant": False}]})
    assert _parse_result(raw, 2) == [True, False]


def test_parse_result_missing_idx_defaults_true():
    # idx 2 absent → defaults to True
    raw = json.dumps({"results": [{"idx": 1, "relevant": False}]})
    assert _parse_result(raw, 2) == [False, True]


def test_parse_result_bad_json():
    assert _parse_result("not json", 3) is None


def test_parse_result_no_results_key():
    raw = json.dumps({"answer": []})
    assert _parse_result(raw, 2) is None


def test_parse_result_json_embedded_in_prose():
    # LLM sometimes wraps JSON in prose
    raw = 'Here is the result: {"results": [{"idx": 1, "relevant": false}]} done.'
    result = _parse_result(raw, 1)
    assert result == [False]


# ---------------------------------------------------------------------------
# filter_by_sufficiency — flag-off fast path
# ---------------------------------------------------------------------------

def _make_home_no_gate(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"sufficiency_gate": False}))
    return str(tmp_path)


def _make_home_gate_on(tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"sufficiency_gate": True}))
    return str(tmp_path)


def test_gate_off_returns_hits_unchanged(tmp_path):
    home = _make_home_no_gate(tmp_path)
    hits = [{"doc_id": "a", "text": "foo", "score": 0.9}]
    result = filter_by_sufficiency("any query", hits, home=home)
    assert result == hits


def test_gate_on_cli_unavailable_fail_open(tmp_path):
    """When the claude CLI can't be found, all hits are returned unchanged."""
    home = _make_home_gate_on(tmp_path)
    hits = [{"doc_id": "a", "text": "budget report", "score": 0.9},
            {"doc_id": "b", "text": "camp roster", "score": 0.7}]
    with patch("mcpbrain.sufficiency._call_claude", return_value=""):
        result = filter_by_sufficiency("what is the camp roster?", hits, home=home)
    assert result == hits  # fail-open


def test_gate_on_filters_irrelevant_hit(tmp_path):
    """A hit marked irrelevant=false is removed; relevant hits are kept."""
    home = _make_home_gate_on(tmp_path)
    hits = [
        {"doc_id": "relevant", "text": "The camp roster has Josh as site director.", "score": 0.9},
        {"doc_id": "irrelevant", "text": "Camp activities include swimming and hiking.", "score": 0.88},
    ]
    response = json.dumps({"results": [{"idx": 1, "relevant": True}, {"idx": 2, "relevant": False}]})
    with patch("mcpbrain.sufficiency._call_claude", return_value=response):
        result = filter_by_sufficiency("who is the camp site director?", hits, home=home)
    assert len(result) == 1
    assert result[0]["doc_id"] == "relevant"


def test_gate_high_similarity_but_irrelevant_withheld(tmp_path):
    """Acceptance criterion from #18: a high-similarity but irrelevant memory is NOT injected.

    Scenario: query asks about budget figures; a chunk about 'budget review meetings'
    is topically similar (both mention 'budget') but contains no figures — irrelevant.
    A chunk with actual figures is relevant.
    """
    home = _make_home_gate_on(tmp_path)
    hits = [
        # High vector score — mentions budget — but no figures
        {"doc_id": "meeting-notes", "text": "Budget review meeting scheduled for Tuesday.", "score": 0.92},
        # Contains actual budget figures
        {"doc_id": "budget-doc", "text": "Ministry budget: income $240,000 expenditure $220,000.", "score": 0.85},
    ]
    response = json.dumps({"results": [{"idx": 1, "relevant": False}, {"idx": 2, "relevant": True}]})
    with patch("mcpbrain.sufficiency._call_claude", return_value=response):
        result = filter_by_sufficiency("what are the ministry budget figures?", hits, home=home)
    doc_ids = [r["doc_id"] for r in result]
    assert "budget-doc" in doc_ids
    assert "meeting-notes" not in doc_ids


def test_gate_genuine_relevant_memory_fires(tmp_path):
    """Acceptance criterion from #18: genuinely relevant memories are NOT withheld."""
    home = _make_home_gate_on(tmp_path)
    hits = [
        {"doc_id": "correct", "text": "Josh confirmed he will lead the Sunday 9am service.", "score": 0.91},
    ]
    response = json.dumps({"results": [{"idx": 1, "relevant": True}]})
    with patch("mcpbrain.sufficiency._call_claude", return_value=response):
        result = filter_by_sufficiency("who is leading the Sunday service?", hits, home=home)
    assert len(result) == 1
    assert result[0]["doc_id"] == "correct"


def test_gate_never_returns_empty_list(tmp_path):
    """If ALL hits are marked irrelevant, the gate fails open and returns all hits.

    This prevents a situation where a query returns nothing due to an over-aggressive
    LLM response — it's better to inject something than to inject nothing and leave
    the model without any grounding.
    """
    home = _make_home_gate_on(tmp_path)
    hits = [
        {"doc_id": "a", "text": "foo", "score": 0.9},
        {"doc_id": "b", "text": "bar", "score": 0.8},
    ]
    response = json.dumps({"results": [{"idx": 1, "relevant": False}, {"idx": 2, "relevant": False}]})
    with patch("mcpbrain.sufficiency._call_claude", return_value=response):
        result = filter_by_sufficiency("some query", hits, home=home)
    # Must not return empty — fail-open when all filtered
    assert len(result) == len(hits)


def test_gate_bad_llm_json_fail_open(tmp_path):
    """Unparseable LLM response → all hits returned unchanged."""
    home = _make_home_gate_on(tmp_path)
    hits = [{"doc_id": "a", "text": "text", "score": 0.9}]
    with patch("mcpbrain.sufficiency._call_claude", return_value="oops not json"):
        result = filter_by_sufficiency("query", hits, home=home)
    assert result == hits


def test_gate_empty_hits_returns_immediately(tmp_path):
    home = _make_home_gate_on(tmp_path)
    assert filter_by_sufficiency("query", [], home=home) == []
