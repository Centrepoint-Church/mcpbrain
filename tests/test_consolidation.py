"""Tests for B4 consolidation pass (cluster → semantic note)."""
import json
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
def home_consolidation(tmp_path):
    """Home dir with consolidation enabled."""
    h = tmp_path / "home-c"
    h.mkdir()
    (h / "config.json").write_text(json.dumps({"consolidation": True}))
    return str(h)


# ---------------------------------------------------------------------------
# _token_set and _cluster_chunks (unit)
# ---------------------------------------------------------------------------

def test_token_set_filters_short_words():
    """Words under 4 chars are ignored."""
    from mcpbrain.consolidation import _token_set
    ts = _token_set("the cat sat on a budget spreadsheet")
    assert "budget" in ts
    assert "spreadsheet" in ts
    assert "the" not in ts
    assert "cat" not in ts


def test_cluster_chunks_groups_similar():
    """Chunks with shared vocabulary are clustered together."""
    from mcpbrain.consolidation import _cluster_chunks

    chunks = [
        {"doc_id": f"a-{i}", "text": "budget review forecast financial planning quarterly"}
        for i in range(4)
    ] + [
        {"doc_id": "b-0", "text": "personal dentist appointment health insurance claim"}
    ]

    clusters = _cluster_chunks(chunks, threshold=0.1)
    # The four budget chunks should cluster together; b-0 is unrelated
    assert len(clusters) >= 1
    budget_cluster = clusters[0]
    ids = [c["doc_id"] for c in budget_cluster]
    assert all(i.startswith("a-") for i in ids)


def test_cluster_chunks_min_size():
    """Clusters smaller than _MIN_CLUSTER_SIZE are dropped."""
    from mcpbrain.consolidation import _cluster_chunks, _MIN_CLUSTER_SIZE

    # Two identical chunks → one cluster of size 2 (< 3 default minimum)
    chunks = [
        {"doc_id": "x", "text": "planning review forecast quarterly"},
        {"doc_id": "y", "text": "planning review forecast quarterly"},
    ]
    clusters = _cluster_chunks(chunks)
    # Both should be in ONE cluster but cluster size < min → dropped
    assert len(clusters) == 0


# ---------------------------------------------------------------------------
# should_consolidate
# ---------------------------------------------------------------------------

def test_should_consolidate_false_when_flag_off(store, tmp_path):
    """should_consolidate is False when flag is disabled."""
    from mcpbrain.consolidation import should_consolidate

    home_off = str(tmp_path / "home-coff")
    os.makedirs(home_off)
    (Path(home_off) / "config.json").write_text(json.dumps({"consolidation": False}))

    # Seed enough salience
    for i in range(10):
        store.upsert_chunk(f"ep-{i}", "budget quarterly review financial forecast", f"h{i}", {})
        with store._connect() as db:
            db.execute(f"UPDATE chunks SET embedded=1, salience=6.0, memory_type='episodic' WHERE doc_id='ep-{i}'")

    assert not should_consolidate(store, home_off)


def test_should_consolidate_true_when_threshold_met(store, home_consolidation):
    """should_consolidate is True when accumulated salience ≥ threshold."""
    from mcpbrain.consolidation import should_consolidate, CONSOLIDATION_THRESHOLD

    needed = int(CONSOLIDATION_THRESHOLD / 5.0) + 1
    for i in range(needed):
        store.upsert_chunk(f"ep-{i}", f"text chunk {i}", f"h{i}", {})
        with store._connect() as db:
            db.execute(f"UPDATE chunks SET embedded=1, salience=5.0, memory_type='episodic' WHERE doc_id='ep-{i}'")

    assert should_consolidate(store, home_consolidation)


# ---------------------------------------------------------------------------
# consolidate (with mocked claude CLI)
# ---------------------------------------------------------------------------

def _seed_cluster(store, prefix: str, n: int = 4, salience: float = 6.0):
    """Seed n related episodic chunks."""
    for i in range(n):
        store.upsert_chunk(
            f"{prefix}-{i}",
            "budget quarterly review financial forecast planning annual report",
            f"h-{prefix}-{i}",
            {},
        )
        with store._connect() as db:
            db.execute(
                f"UPDATE chunks SET embedded=1, salience={salience}, "
                f"memory_type='episodic', memory_tier='warm' WHERE doc_id='{prefix}-{i}'"
            )


def test_consolidate_writes_semantic_note(store, home_consolidation, monkeypatch):
    """consolidate() writes a semantic note chunk via mocked claude CLI."""
    from mcpbrain import consolidation

    _seed_cluster(store, "bud", n=4)

    monkeypatch.setattr(consolidation, "_call_claude",
                        lambda prompt, timeout=60: "Q3 budget review: finance team committed to reducing spend. [bud-0] [bud-1]")

    result = consolidation.consolidate(store, home_consolidation, threshold=0.0)

    assert result["notes_written"] >= 1
    # A semantic note chunk now exists
    with store._connect() as db:
        rows = db.execute(
            "SELECT doc_id, memory_type, memory_tier FROM chunks WHERE memory_type='semantic'"
        ).fetchall()
    assert len(rows) >= 1
    assert rows[0]["memory_tier"] == "hot"


def test_consolidate_marks_sources_hot(store, home_consolidation, monkeypatch):
    """Source chunks are promoted to hot after consolidation."""
    from mcpbrain import consolidation

    _seed_cluster(store, "src", n=4)

    monkeypatch.setattr(consolidation, "_call_claude",
                        lambda prompt, timeout=60: "Summary of source material. [src-0] [src-1]")

    consolidation.consolidate(store, home_consolidation, threshold=0.0)

    with store._connect() as db:
        rows = db.execute(
            "SELECT memory_tier FROM chunks WHERE doc_id LIKE 'src-%'"
        ).fetchall()
    for r in rows:
        assert r["memory_tier"] == "hot", f"Expected hot, got {r['memory_tier']}"


def test_consolidate_skips_when_claude_returns_empty(store, home_consolidation, monkeypatch):
    """If claude returns empty string, no note is written (no crash)."""
    from mcpbrain import consolidation

    _seed_cluster(store, "cl0", n=4)

    monkeypatch.setattr(consolidation, "_call_claude", lambda prompt, timeout=60: "")

    result = consolidation.consolidate(store, home_consolidation, threshold=0.0)
    assert result["notes_written"] == 0


def test_consolidate_noop_when_disabled(store, tmp_path, monkeypatch):
    """consolidate() returns zeros when flag is off."""
    from mcpbrain import consolidation

    home_off = str(tmp_path / "c-off")
    os.makedirs(home_off)
    (Path(home_off) / "config.json").write_text(json.dumps({"consolidation": False}))

    monkeypatch.setattr(consolidation, "_call_claude", lambda prompt, timeout=60: "Summary.")

    _seed_cluster(store, "no", n=4)
    result = consolidation.consolidate(store, home_off, threshold=0.0)
    assert result["notes_written"] == 0
    assert result["clusters_found"] == 0


# --- B4 fix: embedding-based clustering (reuses bge vectors) ---

class _FakeEmbedder:
    """Deterministic unit-norm vectors: 'budget' texts → axis 0, 'roster' → axis 1."""
    def embed_passages(self, texts):
        out = []
        for t in texts:
            tl = (t or "").lower()
            if "budget" in tl:
                out.append([1.0, 0.0, 0.0])
            elif "roster" in tl:
                out.append([0.0, 1.0, 0.0])
            else:
                out.append([0.0, 0.0, 1.0])
        return out


def test_cluster_uses_embeddings_when_embedder_given():
    from mcpbrain.consolidation import _cluster
    chunks = [
        {"doc_id": "a", "text": "the budget review for 2026"},
        {"doc_id": "b", "text": "annual budget planning meeting"},
        {"doc_id": "c", "text": "budget approval next steps"},
        {"doc_id": "d", "text": "sunday roster volunteers"},
        {"doc_id": "e", "text": "roster for the worship team"},
        {"doc_id": "f", "text": "roster sign-up sheet"},
    ]
    clusters = _cluster(chunks, _FakeEmbedder())
    # two semantic clusters (budget vs roster), each >= _MIN_CLUSTER_SIZE (3)
    sizes = sorted(len(c) for c in clusters)
    assert sizes == [3, 3]
    texts = [" ".join(x["text"] for x in cl).lower() for cl in clusters]
    assert any("budget" in t and "roster" not in t for t in texts)


def test_cluster_falls_back_to_lexical_when_embedder_fails():
    from mcpbrain.consolidation import _cluster
    class _Broken:
        def embed_passages(self, texts):
            raise RuntimeError("no embedder")
    chunks = [{"doc_id": str(i), "text": "budget review meeting agenda"} for i in range(3)]
    clusters = _cluster(chunks, _Broken())   # falls back to Jaccard, no raise
    assert len(clusters) == 1 and len(clusters[0]) == 3
