"""Tests for S2 recall-acceptance feedback (feedback.py + store feedback methods)."""
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "test.sqlite3", dim=4)
    s.init()
    # Seed a chunk so we can log feedback against it.
    s.upsert_chunk("doc-a", "Alpha text", "h1", {})
    s.upsert_chunk("doc-b", "Beta text", "h2", {})
    return s


def test_record_feedback_and_retrieve(store):
    """record_recall_feedback writes a row; all_feedback_rows returns it."""
    from mcpbrain.feedback import record_feedback
    record_feedback(store, "doc-a", "sess-1", "exposure")
    rows = store.all_feedback_rows()
    assert len(rows) == 1
    assert rows[0]["doc_id"] == "doc-a"
    assert rows[0]["event_type"] == "exposure"


def test_record_exposures_batch(store):
    """record_exposures logs one row per doc_id."""
    from mcpbrain.feedback import record_exposures
    record_exposures(store, ["doc-a", "doc-b"], "sess-x")
    rows = store.all_feedback_rows()
    assert len(rows) == 2
    assert {r["doc_id"] for r in rows} == {"doc-a", "doc-b"}


def test_exposure_only_stays_neutral(store):
    """Boost-only model: exposure WITHOUT a positive signal must NOT lower quality.

    This guards the original bug where quality = 1/(2+exposures) penalised every
    exposed chunk. With no 'used'/'edited' events, aggregate writes no row and
    quality stays at the 1.0 neutral default.
    """
    from mcpbrain.feedback import record_exposures, aggregate_feedback

    record_exposures(store, ["doc-a", "doc-a", "doc-b"], "sess-1")
    summary = aggregate_feedback(store)

    assert summary["updated"] == 0      # nothing written — all exposure-only
    assert summary["neutral"] >= 1
    assert store.get_chunk_quality("doc-a") == 1.0
    assert store.get_chunk_quality("doc-b") == 1.0


def test_positive_signal_boosts_above_neutral(store):
    """A 'used' event lifts quality above 1.0 (boost-only), never below."""
    from mcpbrain.feedback import record_feedback, record_exposures, aggregate_feedback

    record_exposures(store, ["doc-a", "doc-b"], "sess-1")
    record_feedback(store, "doc-a", "sess-1", "used")
    summary = aggregate_feedback(store)

    assert summary["updated"] == 1      # only doc-a got a positive signal
    q_a = store.get_chunk_quality("doc-a")
    q_b = store.get_chunk_quality("doc-b")
    assert q_a > 1.0                    # boosted
    assert q_b == 1.0                   # exposure-only → neutral


def test_chunk_quality_defaults_to_neutral(store):
    """Chunks with no feedback row return quality=1.0 (neutral ranking signal)."""
    q = store.get_chunk_quality("doc-a")
    # Before any feedback, the quality is neutral (1.0).
    assert q == 1.0


def test_aggregate_empty_store(store):
    """Aggregation on an empty feedback table returns zeros without error."""
    from mcpbrain.feedback import aggregate_feedback
    result = aggregate_feedback(store)
    assert result == {"updated": 0, "skipped": 0, "neutral": 0}


def test_apply_quality_multiplier_neutral(store):
    """weight=0.0 leaves scores unchanged (neutral default)."""
    from mcpbrain.feedback import apply_quality_multiplier
    results = [{"doc_id": "doc-a", "score": 0.8}, {"doc_id": "doc-b", "score": 0.5}]
    out = apply_quality_multiplier(results, store, weight=0.0)
    assert out[0]["score"] == pytest.approx(0.8)
    assert out[1]["score"] == pytest.approx(0.5)


def test_apply_quality_multiplier_boosts(store):
    """weight=1.0 boosts a result by its (>=1.0) chunk_quality."""
    from mcpbrain.feedback import apply_quality_multiplier
    store.update_chunk_quality("doc-a", 1.2, 5, 3)   # boosted chunk
    results = [{"doc_id": "doc-a", "score": 1.0}]
    out = apply_quality_multiplier(results, store, weight=1.0)
    # score * (1 + 1.0 * (1.2 - 1.0)) = 1.0 * 1.2 = 1.2
    assert out[0]["score"] == pytest.approx(1.2, abs=0.01)
