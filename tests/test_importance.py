"""Tests for B3 importance scoring + three-axis recall."""
import math
import tempfile
from pathlib import Path

import pytest


@pytest.fixture
def store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "test.sqlite3", dim=4)
    s.init()
    return s


# ---------------------------------------------------------------------------
# score_structural
# ---------------------------------------------------------------------------

def test_baseline_score():
    """Unknown metadata → baseline score in [1.0, 10.0]."""
    from mcpbrain.importance import score_structural
    s = score_structural({})
    assert 1.0 <= s <= 10.0


def test_reply_depth_boosts():
    """Higher reply depth → higher score."""
    from mcpbrain.importance import score_structural
    s0 = score_structural({"reply_depth": 0})
    s1 = score_structural({"reply_depth": 1})
    s2 = score_structural({"reply_depth": 2})
    assert s2 >= s1 >= s0


def test_sender_is_owner_boosts():
    """Josh-sent content scores higher than generic."""
    from mcpbrain.importance import score_structural
    base = score_structural({})
    owned = score_structural({"sender_is_owner": True})
    assert owned > base


def test_noreply_penalty():
    """No-reply sender is penalised."""
    from mcpbrain.importance import score_structural
    base = score_structural({})
    noreply = score_structural({"sender": "noreply@example.com"})
    assert noreply < base


def test_promotions_penalty():
    """CATEGORY_PROMOTIONS label lowers score."""
    from mcpbrain.importance import score_structural
    base = score_structural({})
    promo = score_structural({"labels": ["CATEGORY_PROMOTIONS"]})
    assert promo < base


def test_starred_boosts():
    """Starred label boosts score."""
    from mcpbrain.importance import score_structural
    base = score_structural({})
    starred = score_structural({"labels": ["starred"]})
    assert starred > base


def test_score_clamped():
    """Score is always in [1.0, 10.0] even with extreme inputs."""
    from mcpbrain.importance import score_structural
    extreme = score_structural({
        "sender_is_owner": True,
        "reply_depth": 5,
        "labels": ["starred", "IMPORTANT"],
        "entities": ["a", "b", "c", "d", "e", "f"],
    })
    assert 1.0 <= extreme <= 10.0


# ---------------------------------------------------------------------------
# recency_decay
# ---------------------------------------------------------------------------

def test_recency_decay_today():
    """Brand-new content (age ~0 days) → R ~ 1.0."""
    from mcpbrain.importance import recency_decay
    from datetime import datetime, timezone
    # Use YYYY-MM-DD so _parse_age_days picks up the %Y-%m-%d format path
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    r = recency_decay({"date_iso": today})
    # age <= 1 day → R = exp(-0.01 * age) >= exp(-0.01) ≈ 0.99
    assert r > 0.98


def test_recency_decay_old():
    """Old content (age >> half-life) → R << 1."""
    from mcpbrain.importance import recency_decay
    r = recency_decay({"date": "Mon, 01 Jan 2024 00:00:00 +0000"})
    assert r < 0.5


def test_recency_decay_monotone():
    """Older content always decays more than newer."""
    from mcpbrain.importance import recency_decay
    recent = recency_decay({"date": "Mon, 01 Jun 2026 00:00:00 +0000"})
    old = recency_decay({"date": "Mon, 01 Jan 2024 00:00:00 +0000"})
    assert recent > old


# ---------------------------------------------------------------------------
# run_salience_pass
# ---------------------------------------------------------------------------

def test_run_salience_pass_scores_chunks(store):
    """run_salience_pass scores unscored embedded chunks."""
    import json
    from mcpbrain.importance import run_salience_pass

    # Seed two embedded chunks with salience=0
    store.upsert_chunk("doc-1", "Meeting about budget decisions", "h1",
                       {"sender": "bob@example.com", "reply_depth": 1})
    store.upsert_chunk("doc-2", "Newsletter unsubscribe promotion", "h2",
                       {"sender": "noreply@news.com", "labels": ["CATEGORY_PROMOTIONS"]})
    # Mark as embedded (normally the embedder does this)
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1")

    result = run_salience_pass(store, "/tmp/test-home")
    assert result["scored"] == 2
    assert result["llm_scored"] == 0   # importance_llm flag off → structural only

    s1 = store.get_chunk_salience("doc-1")
    s2 = store.get_chunk_salience("doc-2")
    assert s1 > 0.0
    assert s2 > 0.0
    # The meeting-with-reply should score higher than a promo newsletter
    assert s1 > s2


def test_run_salience_pass_skips_unembedded(store):
    """run_salience_pass only touches embedded=1 chunks."""
    from mcpbrain.importance import run_salience_pass

    store.upsert_chunk("doc-unembedded", "Some text", "h1", {})
    # embedded=0 by default
    result = run_salience_pass(store, "/tmp/test-home")
    assert result["scored"] == 0


def test_run_salience_pass_skips_already_scored(store):
    """run_salience_pass skips chunks already scored (salience > 0)."""
    from mcpbrain.importance import run_salience_pass

    store.upsert_chunk("doc-a", "Already scored", "h1", {})
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1, salience=5.0")

    result = run_salience_pass(store, "/tmp/test-home")
    assert result["scored"] == 0


# ---------------------------------------------------------------------------
# three-axis ranking in hybrid_search
# ---------------------------------------------------------------------------

def _make_store_with_embedder(tmp_path):
    """Build a store + a fake embedder that gives controllable vectors."""
    from mcpbrain.store import Store

    class FakeEmbedder:
        def embed_query(self, q):
            return [1.0, 0.0, 0.0, 0.0]

        def embed_passages(self, texts):
            return [[1.0, 0.0, 0.0, 0.0]] * len(texts)

    s = Store(tmp_path / "t.sqlite3", dim=4)
    s.init()
    return s, FakeEmbedder()


def test_importance_weight_reorders(tmp_path):
    """A high-salience chunk should rank above a low-salience one when
    importance_weight > 0, even if RRF scores are identical."""
    import sqlite_vec
    from mcpbrain.store import Store
    from mcpbrain.retrieval import hybrid_search

    store, emb = _make_store_with_embedder(tmp_path)

    # Two identical-text chunks → identical RRF scores.
    store.upsert_chunk("doc-lo", "budget review", "h1", {})
    store.upsert_chunk("doc-hi", "budget review", "h2", {})
    # Set different salience scores
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1, salience=2.0 WHERE doc_id='doc-lo'")
        db.execute("UPDATE chunks SET embedded=1, salience=9.0 WHERE doc_id='doc-hi'")
    # Embed both with the same vector
    for doc_id in ("doc-lo", "doc-hi"):
        with store._connect() as db:
            row = db.execute("SELECT rowid FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
        store.write_embedding(row["rowid"], [1.0, 0.0, 0.0, 0.0])

    results_no_imp = hybrid_search(store, emb, "budget")
    results_with_imp = hybrid_search(store, emb, "budget", importance_weight=0.5)

    # Without importance weighting, both chunks appear (order not guaranteed)
    doc_ids_no_imp = [r["doc_id"] for r in results_no_imp]
    assert "doc-hi" in doc_ids_no_imp and "doc-lo" in doc_ids_no_imp

    # With importance weighting, doc-hi (salience=9) must rank first
    doc_ids_imp = [r["doc_id"] for r in results_with_imp]
    assert doc_ids_imp[0] == "doc-hi", (
        f"Expected doc-hi first, got {doc_ids_imp}"
    )


def test_exclude_cold_filters_cold_chunks(tmp_path):
    """exclude_cold=True must exclude memory_tier='cold' chunks."""
    from mcpbrain.retrieval import hybrid_search

    store, emb = _make_store_with_embedder(tmp_path)

    store.upsert_chunk("doc-warm", "budget review", "h1", {})
    store.upsert_chunk("doc-cold", "budget review", "h2", {})
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1")
        db.execute("UPDATE chunks SET memory_tier='cold' WHERE doc_id='doc-cold'")
    for doc_id in ("doc-warm", "doc-cold"):
        with store._connect() as db:
            row = db.execute("SELECT rowid FROM chunks WHERE doc_id=?", (doc_id,)).fetchone()
        store.write_embedding(row["rowid"], [1.0, 0.0, 0.0, 0.0])

    results = hybrid_search(store, emb, "budget", exclude_cold=True)
    doc_ids = [r["doc_id"] for r in results]
    assert "doc-cold" not in doc_ids
    assert "doc-warm" in doc_ids


# --- B3 fix: LLM poignancy (claude CLI) ---

def test_score_llm_returns_none_without_claude(monkeypatch):
    import mcpbrain.importance as imp
    from mcpbrain import config
    monkeypatch.setattr(config, "find_claude", lambda: (_ for _ in ()).throw(RuntimeError("no cli")))
    assert imp.score_llm("some text") is None


def test_salience_pass_blends_llm_when_enabled(monkeypatch, tmp_path):
    """With importance_llm on, the top structural item gets an LLM-blended score."""
    import json
    import mcpbrain.importance as imp
    from mcpbrain.store import Store
    s = Store(tmp_path / "b.sqlite3", dim=4); s.init()
    s.upsert_chunk("d1", "Major budget decision by the board", "h", {"sender": "josh@x.com"})
    with s._connect() as db:
        db.execute("UPDATE chunks SET embedded=1")
    home = tmp_path / "home"; home.mkdir()
    (home / "config.json").write_text(json.dumps({"importance_llm": True}))
    monkeypatch.setattr(imp, "score_llm", lambda text, **k: 10.0)   # stub the CLI call
    result = imp.run_salience_pass(s, str(home))
    assert result["llm_scored"] >= 1
    # blended = 0.6*structural + 0.4*10 → strictly above pure structural baseline
    assert s.get_chunk_salience("d1") > 3.0
