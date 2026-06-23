"""Tests for B2 tiered memory (core block, tier promotion/demotion)."""
import json
import pytest
from pathlib import Path


@pytest.fixture
def store(tmp_path):
    from mcpbrain.store import Store
    s = Store(tmp_path / "test.sqlite3", dim=4)
    s.init()
    return s


@pytest.fixture
def home(tmp_path):
    """A home dir with tiered_memory enabled."""
    h = tmp_path / "home"
    h.mkdir()
    (h / "config.json").write_text(json.dumps({"tiered_memory": True}))
    return str(h)


# ---------------------------------------------------------------------------
# get_core_block
# ---------------------------------------------------------------------------

def test_core_block_empty_when_no_core_chunks(store, home):
    """No core-tier chunks → empty string."""
    from mcpbrain.memory_tier import get_core_block
    assert get_core_block(store, home) == ""


def test_core_block_contains_core_chunk_text(store, home):
    """Core-tier chunks appear in the core block."""
    from mcpbrain.memory_tier import get_core_block

    store.upsert_chunk("core-1", "Josh is the lead pastor at Centrepoint Church.", "h1", {})
    store.set_chunk_tier("core-1", "core")

    block = get_core_block(store, home)
    assert block != ""
    assert "Centrepoint" in block


def test_core_block_excludes_non_core(store, home):
    """Non-core chunks don't appear in the core block."""
    from mcpbrain.memory_tier import get_core_block

    store.upsert_chunk("core-1", "Core fact.", "h1", {})
    store.set_chunk_tier("core-1", "core")
    store.upsert_chunk("warm-1", "Just a warm episodic note.", "h2", {})
    # warm-1 has no tier set

    block = get_core_block(store, home)
    assert "Core fact" in block
    assert "episodic" not in block


def test_core_block_disabled_when_flag_off(store, tmp_path):
    """core block returns '' when tiered_memory=False."""
    from mcpbrain.memory_tier import get_core_block
    import os

    home_off = str(tmp_path / "home-off")
    os.makedirs(home_off)
    (Path(home_off) / "config.json").write_text(json.dumps({"tiered_memory": False}))

    store.upsert_chunk("core-1", "Core fact.", "h1", {})
    store.set_chunk_tier("core-1", "core")

    assert get_core_block(store, home_off) == ""


# ---------------------------------------------------------------------------
# promote_to_hot / demote_to_cold
# ---------------------------------------------------------------------------

def test_promote_to_hot_from_warm(store):
    """Warm chunk promoted to hot."""
    from mcpbrain.memory_tier import promote_to_hot

    store.upsert_chunk("doc-w", "text", "h1", {})
    store.set_chunk_tier("doc-w", "warm")

    promoted = promote_to_hot(store, ["doc-w"])
    assert promoted == 1

    with store._connect() as db:
        row = db.execute("SELECT memory_tier FROM chunks WHERE doc_id='doc-w'").fetchone()
    assert row["memory_tier"] == "hot"


def test_promote_to_hot_from_untiered(store):
    """Untiered chunk promoted to hot."""
    from mcpbrain.memory_tier import promote_to_hot

    store.upsert_chunk("doc-u", "text", "h1", {})

    promoted = promote_to_hot(store, ["doc-u"])
    assert promoted == 1

    with store._connect() as db:
        row = db.execute("SELECT memory_tier FROM chunks WHERE doc_id='doc-u'").fetchone()
    assert row["memory_tier"] == "hot"


def test_demote_to_cold(store):
    """Chunks are demoted to cold (not deleted)."""
    from mcpbrain.memory_tier import demote_to_cold

    store.upsert_chunk("doc-x", "text", "h1", {})

    demoted = demote_to_cold(store, ["doc-x"])
    assert demoted == 1

    with store._connect() as db:
        row = db.execute("SELECT memory_tier, text FROM chunks WHERE doc_id='doc-x'").fetchone()
    assert row["memory_tier"] == "cold"
    assert row["text"] == "text"   # ADDITIVE: source never deleted


def test_demote_does_not_delete_core(store):
    """demote_to_cold must not demote 'core' chunks."""
    from mcpbrain.memory_tier import demote_to_cold

    store.upsert_chunk("core-safe", "core fact", "h1", {})
    store.set_chunk_tier("core-safe", "core")

    demoted = demote_to_cold(store, ["core-safe"])
    assert demoted == 0

    with store._connect() as db:
        row = db.execute("SELECT memory_tier FROM chunks WHERE doc_id='core-safe'").fetchone()
    assert row["memory_tier"] == "core"


# ---------------------------------------------------------------------------
# run_tier_pass
# ---------------------------------------------------------------------------

def test_run_tier_pass_demotes_low_salience(store, home):
    """Chunks with salience below floor are demoted to cold by run_tier_pass."""
    from mcpbrain.memory_tier import run_tier_pass

    store.upsert_chunk("low-s", "text", "h1", {})
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1, salience=1.0, memory_tier='warm' WHERE doc_id='low-s'")

    result = run_tier_pass(store, home)
    assert result["demoted"] >= 1

    with store._connect() as db:
        row = db.execute("SELECT memory_tier FROM chunks WHERE doc_id='low-s'").fetchone()
    assert row["memory_tier"] == "cold"


def test_run_tier_pass_keeps_high_salience(store, home):
    """High-salience chunks are not demoted."""
    from mcpbrain.memory_tier import run_tier_pass

    store.upsert_chunk("high-s", "text", "h1", {})
    with store._connect() as db:
        db.execute("UPDATE chunks SET embedded=1, salience=8.0, memory_tier='warm' WHERE doc_id='high-s'")

    run_tier_pass(store, home)

    with store._connect() as db:
        row = db.execute("SELECT memory_tier FROM chunks WHERE doc_id='high-s'").fetchone()
    assert row["memory_tier"] == "warm"


# ---------------------------------------------------------------------------
# recompute_core — the promoter that was missing (B2 fix)
# ---------------------------------------------------------------------------

def test_recompute_core_promotes_top_durable_notes(store, home):
    from mcpbrain.memory_tier import recompute_core, get_core_block
    # Durable semantic notes with varying salience + one episodic (must NOT be core).
    store.upsert_chunk("sem-hi", "Centrepoint board decided the 2026 budget.", "h1", {})
    store.set_chunk_type("sem-hi", "semantic"); store.set_chunk_salience("sem-hi", 9.0)
    store.upsert_chunk("sem-lo", "Minor note about a coffee order.", "h2", {})
    store.set_chunk_type("sem-lo", "semantic"); store.set_chunk_salience("sem-lo", 2.0)
    store.upsert_chunk("epi-1", "A raw email thread.", "h3", {})
    store.set_chunk_type("epi-1", "episodic"); store.set_chunk_salience("epi-1", 10.0)

    n = recompute_core(store, home, max_items=1)
    assert n == 1
    core_ids = {c["doc_id"] for c in store.chunks_by_tier("core")}
    assert core_ids == {"sem-hi"}          # highest-salience DURABLE note only
    assert "epi-1" not in core_ids         # episodic email never core
    # and it now shows up in the always-injected block
    assert "budget" in get_core_block(store, home).lower()


def test_recompute_core_demotes_dropouts_to_hot(store, home):
    from mcpbrain.memory_tier import recompute_core
    store.upsert_chunk("old-core", "previously core note", "h", {})
    store.set_chunk_type("old-core", "semantic"); store.set_chunk_salience("old-core", 1.0)
    store.set_chunk_tier("old-core", "core")
    store.upsert_chunk("new-top", "new top semantic note", "h2", {})
    store.set_chunk_type("new-top", "semantic"); store.set_chunk_salience("new-top", 9.0)

    recompute_core(store, home, max_items=1)
    tiers = {c["doc_id"]: "core" for c in store.chunks_by_tier("core")}
    assert "new-top" in tiers and "old-core" not in tiers   # reversible demotion
    assert any(c["doc_id"] == "old-core" for c in store.chunks_by_tier("hot"))


def test_recompute_core_noop_when_flag_off(store, tmp_path):
    from mcpbrain.memory_tier import recompute_core
    h = tmp_path / "off"; h.mkdir(); (h / "config.json").write_text("{}")
    store.upsert_chunk("s1", "x", "h", {}); store.set_chunk_type("s1", "semantic")
    assert recompute_core(store, str(h)) == 0
