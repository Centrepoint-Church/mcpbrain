"""Tests for B5 memory decay and strengthening."""
import json
import math
import os
import pytest
from pathlib import Path


@pytest.fixture
def store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "test.sqlite3", dim=4)
    s.init()
    return s


@pytest.fixture
def home_decay(tmp_path):
    """Home dir with decay enabled."""
    import json, os
    h = tmp_path / "home-decay"
    h.mkdir()
    (h / "config.json").write_text(json.dumps({"decay": True}))
    return str(h)


# ---------------------------------------------------------------------------
# compute_decay
# ---------------------------------------------------------------------------

def test_compute_decay_zero_days():
    """Just-accessed memory has R ~ 1.0."""
    from mcpbrain.decay import compute_decay
    assert compute_decay(5.0, 0.0) == 1.0


def test_compute_decay_positive():
    """Positive days_since always returns R < 1.0 and > 0.0."""
    from mcpbrain.decay import compute_decay
    r = compute_decay(5.0, 10.0)
    assert 0.0 < r < 1.0


def test_compute_decay_formula():
    """R = exp(-days_since / strength)."""
    from mcpbrain.decay import compute_decay
    expected = math.exp(-20.0 / 5.0)
    assert abs(compute_decay(5.0, 20.0) - expected) < 1e-9


def test_compute_decay_strong_memory():
    """High-strength memories decay slower."""
    from mcpbrain.decay import compute_decay
    r_weak = compute_decay(1.0, 10.0)
    r_strong = compute_decay(10.0, 10.0)
    assert r_strong > r_weak


def test_compute_decay_zero_strength_safe():
    """Zero strength falls back to initial default (no divide-by-zero)."""
    from mcpbrain.decay import compute_decay
    r = compute_decay(0.0, 5.0)
    assert 0.0 < r <= 1.0


# ---------------------------------------------------------------------------
# update_on_recall
# ---------------------------------------------------------------------------

def test_update_on_recall_increases_strength(store):
    """Recalled chunks get strength += 1."""
    from mcpbrain.decay import update_on_recall

    store.upsert_chunk("doc-a", "some text", "h1", {})
    # Initial strength is 5.0 (default)
    s0, _ = store.get_memory_strength("doc-a")
    assert s0 == 5.0

    update_on_recall(store, ["doc-a"])
    s1, _ = store.get_memory_strength("doc-a")
    assert s1 == 6.0


def test_update_on_recall_sets_last_accessed(store):
    """update_on_recall writes last_accessed timestamp."""
    from mcpbrain.decay import update_on_recall

    store.upsert_chunk("doc-b", "text", "h1", {})
    _, la0 = store.get_memory_strength("doc-b")
    assert la0 == "" or la0 is None

    update_on_recall(store, ["doc-b"], now="2026-06-01T12:00:00Z")
    _, la1 = store.get_memory_strength("doc-b")
    assert la1 == "2026-06-01T12:00:00Z"


def test_update_on_recall_empty_list(store):
    """update_on_recall with no doc_ids is a safe no-op."""
    from mcpbrain.decay import update_on_recall
    update_on_recall(store, [])  # must not raise


# ---------------------------------------------------------------------------
# apply_decay_pass
# ---------------------------------------------------------------------------

def test_apply_decay_pass_demotes_decayed_chunk(store, home_decay):
    """A chunk with very low R is demoted to cold."""
    from mcpbrain.decay import apply_decay_pass

    store.upsert_chunk("old-doc", "text", "h1", {})
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1, salience=1.0 WHERE doc_id='old-doc'")
    # Set a very old last_accessed and weak strength
    store.update_memory_strength_batch([("old-doc", 1.0, "2020-01-01T00:00:00Z")])

    result = apply_decay_pass(store, home_decay, now="2026-06-23T00:00:00Z")
    assert result["demoted"] >= 1

    with store._connect() as db:
        row = db.execute("SELECT memory_tier FROM chunks WHERE doc_id='old-doc'").fetchone()
    assert row["memory_tier"] == "cold"


def test_apply_decay_pass_preserves_high_salience(store, home_decay):
    """Chunks with salience >= 7.0 are exempt from demotion even with low R."""
    from mcpbrain.decay import apply_decay_pass

    store.upsert_chunk("important-doc", "text", "h1", {})
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1, salience=8.0 WHERE doc_id='important-doc'")
    store.update_memory_strength_batch([("important-doc", 1.0, "2020-01-01T00:00:00Z")])

    result = apply_decay_pass(store, home_decay, now="2026-06-23T00:00:00Z")
    assert result["exempt"] >= 1

    with store._connect() as db:
        row = db.execute("SELECT memory_tier FROM chunks WHERE doc_id='important-doc'").fetchone()
    # Should NOT be demoted to cold
    assert row["memory_tier"] != "cold"


def test_apply_decay_pass_never_deletes(store, home_decay):
    """Decayed chunks remain in the DB — ADDITIVE OVER SOURCE."""
    from mcpbrain.decay import apply_decay_pass

    store.upsert_chunk("deleteme-not", "preserved text", "h1", {})
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1, salience=1.0 WHERE doc_id='deleteme-not'")
    store.update_memory_strength_batch([("deleteme-not", 1.0, "2020-01-01T00:00:00Z")])

    apply_decay_pass(store, home_decay, now="2026-06-23T00:00:00Z")

    with store._connect() as db:
        row = db.execute("SELECT text FROM chunks WHERE doc_id='deleteme-not'").fetchone()
    assert row is not None
    assert row["text"] == "preserved text"


def test_apply_decay_pass_noop_when_disabled(store, tmp_path):
    """apply_decay_pass is a no-op when decay flag is off."""
    from mcpbrain.decay import apply_decay_pass

    home_off = str(tmp_path / "home-off")
    os.makedirs(home_off)
    (Path(home_off) / "config.json").write_text(json.dumps({"decay": False}))

    store.upsert_chunk("doc-nd", "text", "h1", {})
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1, salience=1.0 WHERE doc_id='doc-nd'")
    store.update_memory_strength_batch([("doc-nd", 1.0, "2020-01-01T00:00:00Z")])

    result = apply_decay_pass(store, home_off, now="2026-06-23T00:00:00Z")
    assert result["demoted"] == 0

    with store._connect() as db:
        row = db.execute("SELECT memory_tier FROM chunks WHERE doc_id='doc-nd'").fetchone()
    assert row["memory_tier"] != "cold"
