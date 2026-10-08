"""Store.get_chunks — batched get_chunk/get_chunk_salience for hybrid_search's
collapse_documents candidate pool (task 2b, 2026-10-07).

get_chunks must return, byte-for-byte, what the existing per-id get_chunk /
get_chunk_salience would return for the same ids, just in one connection
instead of one per id.
"""
import pytest

from mcpbrain.retrieval import hybrid_search
from mcpbrain.store import Store


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    return s


def test_matches_get_chunk_for_every_id(store):
    store.upsert_chunk("d1", "hello world", "h1", {"source_type": "gmail", "a": 1})
    store.upsert_chunk("d2", "second chunk", "h2", {"source_type": "note"})
    store.set_chunk_tier("d2", "hot")
    store.set_chunk_salience("d1", 4.5)

    expected = {d: store.get_chunk(d) for d in ("d1", "d2")}
    got = store.get_chunks(["d1", "d2"])

    assert got == expected


def test_with_salience_matches_get_chunk_salience(store):
    store.upsert_chunk("d1", "hello world", "h1", {})
    store.upsert_chunk("d2", "unscored chunk", "h2", {})
    store.set_chunk_salience("d1", 7.25)

    got = store.get_chunks(["d1", "d2"], with_salience=True)

    assert got["d1"]["salience"] == store.get_chunk_salience("d1") == 7.25
    assert got["d2"]["salience"] == store.get_chunk_salience("d2") == 0.0
    # Every key get_chunk itself returns is still present alongside salience.
    base = store.get_chunk("d1")
    for k, v in base.items():
        assert got["d1"][k] == v


def test_without_salience_key_absent(store):
    store.upsert_chunk("d1", "hello world", "h1", {})
    got = store.get_chunks(["d1"])
    assert "salience" not in got["d1"]


def test_missing_id_is_absent(store):
    store.upsert_chunk("d1", "hello world", "h1", {})
    got = store.get_chunks(["d1", "does-not-exist"])
    assert set(got) == {"d1"}


def test_empty_input_returns_empty_dict(store):
    assert store.get_chunks([]) == {}


def test_metadata_is_decoded_to_a_dict(store):
    store.upsert_chunk("d1", "x", "h1", {"nested": {"a": 1}, "list": [1, 2]})
    got = store.get_chunks(["d1"])
    assert got["d1"]["metadata"] == {"nested": {"a": 1}, "list": [1, 2]}


def test_more_than_500_ids_works(store):
    ids = [f"d{i}" for i in range(1200)]
    for i, d in enumerate(ids):
        store.upsert_chunk(d, f"text {i}", f"h{i}", {"i": i})
    for d in ids[::7]:  # scatter some tiers/salience across the batch boundaries
        store.set_chunk_tier(d, "warm")
    store.set_chunk_salience(ids[499], 3.0)   # right at a batch edge
    store.set_chunk_salience(ids[500], 6.0)
    store.set_chunk_salience(ids[1199], 9.0)  # last id, last batch

    got = store.get_chunks(ids, with_salience=True)

    assert len(got) == 1200
    assert got[ids[499]]["salience"] == 3.0
    assert got[ids[500]]["salience"] == 6.0
    assert got[ids[1199]]["salience"] == 9.0
    assert got[ids[0]]["salience"] == 0.0
    assert got[ids[7]]["memory_tier"] == "warm"
    assert got[ids[8]]["memory_tier"] == ""
    # Spot-check full equality against the single-id path too.
    for d in (ids[0], ids[499], ids[500], ids[1199]):
        expected = store.get_chunk(d)
        for k, v in expected.items():
            assert got[d][k] == v


# --- hybrid_search no longer calls get_chunk/get_chunk_salience per candidate -----

class _Emb:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] if "budget" in t else [0, 1.0, 0, 0] for t in texts]

    def embed_query(self, text):
        return [1.0, 0, 0, 0] if "budget" in text else [0, 1.0, 0, 0]


def _seed_many(tmp_path, n):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for i in range(n):
        s.upsert_chunk(f"gdrive-F{i}-0", f"budget section {i}", f"h{i}",
                       {"file_id": f"F{i}"})
    from mcpbrain.index import index_pending
    index_pending(s, _Emb())
    return s


def test_hybrid_search_never_calls_get_chunk_per_candidate(tmp_path, monkeypatch):
    s = _seed_many(tmp_path, 30)

    def boom(*a, **kw):
        raise AssertionError("get_chunk must not be called per-candidate anymore")

    monkeypatch.setattr(Store, "get_chunk", boom)
    monkeypatch.setattr(Store, "get_chunk_salience", boom)

    out = hybrid_search(s, _Emb(), "budget", limit=10, collapse_documents=True,
                        importance_weight=1.0)
    assert len(out) == 10


def test_hybrid_search_connection_count_is_bounded(tmp_path, monkeypatch):
    s = _seed_many(tmp_path, 30)

    original_connect = Store._connect
    calls = {"n": 0}

    def counting_connect(self, *a, **kw):
        calls["n"] += 1
        return original_connect(self, *a, **kw)

    monkeypatch.setattr(Store, "_connect", counting_connect)

    out = hybrid_search(s, _Emb(), "budget", limit=10, collapse_documents=True)

    assert len(out) > 0
    assert calls["n"] <= 6, f"expected a bounded connection count, got {calls['n']}"


def test_pre_phase2_schema_falls_back_like_get_chunk(tmp_path):
    """A raw `chunks` table with neither memory_tier nor salience (the
    upgrade -> init() migration window): get_chunks degrades exactly as
    get_chunk / get_chunk_salience do -- memory_tier "" and salience 0.0 --
    instead of raising."""
    import json
    import sqlite3
    path = tmp_path / "old.sqlite3"
    raw = sqlite3.connect(path)
    raw.execute("CREATE TABLE chunks (doc_id TEXT PRIMARY KEY, text TEXT,"
                " metadata TEXT, content_hash TEXT)")
    raw.executemany("INSERT INTO chunks VALUES (?,?,?,?)",
                    [("d1", "first", json.dumps({"a": 1}), "h1"),
                     ("d2", "second", json.dumps({}), "h2")])
    raw.commit()
    raw.close()
    s = Store(path, dim=4)          # deliberately NOT init(): no migration

    got = s.get_chunks(["d1", "d2", "missing"], with_salience=True)

    assert set(got) == {"d1", "d2"}
    for d in ("d1", "d2"):
        assert got[d]["memory_tier"] == ""
        assert got[d]["salience"] == 0.0 == s.get_chunk_salience(d)
        assert {k: v for k, v in got[d].items() if k != "salience"} == s.get_chunk(d)
    assert s.get_chunks(["d1"]) == {"d1": s.get_chunk("d1")}
    # The fallbacks really ran: nothing migrated the table behind our back.
    cols = {r[1] for r in sqlite3.connect(path).execute("PRAGMA table_info(chunks)")}
    assert not cols & {"memory_tier", "salience"}
