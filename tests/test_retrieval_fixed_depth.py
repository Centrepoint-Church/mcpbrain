"""Fixed-depth fusion (0.7.142): hybrid_search's ranking does not depend on
the caller's `limit`, one-result-per-document is a pure post-filter over that
ranking, and the digest-vs-raw rule depends on what is RETURNED, never on
pool-depth arithmetic."""
import hashlib

from mcpbrain.retrieval import (FUSION_DEPTH, _collapse_documents,
                                _document_key, _dedupe_by_cluster, hybrid_search)
from mcpbrain.store import Store


class _Emb:
    """Deterministic, text-dependent 4-d vectors so the vector ranker and the
    keyword ranker disagree (otherwise RRF depth would not matter)."""
    dim = 4

    @staticmethod
    def _vec(text):
        h = hashlib.sha256(text.encode()).digest()
        return [1.0 + h[0] / 64, h[1] / 255, h[2] / 255, h[3] / 255]

    def embed_passages(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return [1.0, 0.0, 0.0, 0.0]


_FILES = 70


def _seed(tmp_path):
    """70 Drive files, every third one with three chunks: ~116 chunks, well
    over 2 * 20, all matching the keyword 'budget' to varying degrees."""
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    for f in range(_FILES):
        n = 3 if f % 3 == 0 else 1
        for i in range(n):
            text = "budget " * (1 + (f * 7 + i) % 5) + f"line {f} part {i} " + \
                   " ".join(f"w{(f * 13 + i * 5 + k) % 37}" for k in range(f % 9))
            s.upsert_chunk(f"gdrive-F{f}-{i}", text, f"h{f}-{i}",
                           {"file_id": f"F{f}"})
    from mcpbrain.index import index_pending
    index_pending(s, _Emb())
    return s


def _ids(hits):
    return [h["doc_id"] for h in hits]


def test_fusion_depth_is_100():
    assert FUSION_DEPTH == 100


def test_ranking_is_a_prefix_across_limits_collapse_off_and_on(tmp_path):
    s = _seed(tmp_path)
    for collapse in (False, True):
        r5, r10, r20 = (_ids(hybrid_search(s, _Emb(), "budget", limit=n,
                                           collapse_documents=collapse))
                        for n in (5, 10, 20))
        assert len(r5) == 5 and len(r10) == 10 and len(r20) == 20
        assert r10[:5] == r5, collapse
        assert r20[:10] == r10, collapse


def test_collapse_never_reorders(tmp_path):
    s = _seed(tmp_path)
    off = hybrid_search(s, _Emb(), "budget", limit=FUSION_DEPTH // 2)
    on = hybrid_search(s, _Emb(), "budget", limit=20, collapse_documents=True)
    off_ids = _ids(off)
    # Every kept hit is its document's FIRST hit in the collapse-off ranking...
    first = {}
    for h in off:
        first.setdefault(_document_key(h), h["doc_id"])
    assert all(first[_document_key(h)] == h["doc_id"] for h in on)
    # ...and the kept hits keep their collapse-off relative order.
    pos = [off_ids.index(d) for d in _ids(on)]
    assert pos == sorted(pos)
    # It is exactly the post-filter: collapse the collapse-off ranking, cut.
    assert _ids(on) == _ids(_collapse_documents(
        hybrid_search(s, _Emb(), "budget", limit=FUSION_DEPTH // 2)))[:20]


def _spy_depths(s):
    depths = []
    real_vec, real_fts = s.vec_knn, s.fts_search

    def vec(qv, k):
        depths.append(("vec", k))
        return real_vec(qv, k)

    def fts(q, k):
        depths.append(("fts", k))
        return real_fts(q, k)

    s.vec_knn, s.fts_search = vec, fts
    return depths


def test_ranker_depth_is_fusion_depth_for_limit_10_both_modes(tmp_path):
    s = _seed(tmp_path)
    depths = _spy_depths(s)
    hybrid_search(s, _Emb(), "budget", limit=10)
    hybrid_search(s, _Emb(), "budget", limit=10, collapse_documents=True)
    assert depths == [("vec", FUSION_DEPTH), ("fts", FUSION_DEPTH)] * 2


def test_limit_above_half_the_depth_grows_the_depth(tmp_path):
    s = _seed(tmp_path)
    depths = _spy_depths(s)
    limit = FUSION_DEPTH // 2 + 10
    assert len(hybrid_search(s, _Emb(), "budget", limit=limit)) == limit
    assert depths == [("vec", limit * 2), ("fts", limit * 2)]
    assert len(hybrid_search(s, _Emb(), "budget", limit=_FILES,
                             collapse_documents=True)) == _FILES


def test_daemon_recall_knn_uses_fusion_depth(tmp_path, monkeypatch):
    import mcpbrain.daemon as daemon_mod
    from mcpbrain.daemon import Daemon, SingleWriterLock
    monkeypatch.setenv("MCPBRAIN_HOME", str(tmp_path))
    asked = []

    class _Store:
        def vec_knn(self, qv, k):
            asked.append(k)
            return [("d1", 0.5)]

    class _E:
        dim = 4

        def embed_query(self, text):
            return [0.0] * 4

    monkeypatch.setattr(daemon_mod, "hybrid_search",
                        lambda *a, **k: [{"doc_id": "d1", "score": 1.0, "text": "x"}])
    d = Daemon(_Store(), _E(), services={}, lock=SingleWriterLock(tmp_path / "d.lock"))
    assert d.search("q", limit=5)[0]["distance"] == 0.5
    assert asked == [FUSION_DEPTH]


# -- the digest-vs-raw rule ---------------------------------------------------

def _hit(doc_id, **meta):
    return {"doc_id": doc_id, "metadata": meta}


def _pool(sibling_at=None, n=60, drive=False):
    """A #1 digest for cluster T, optionally its only raw sibling at
    `sibling_at`, unrelated filler everywhere else."""
    if drive:
        hits = [_hit("enriched-T", file_id="T")]
        sib = _hit("gdrive-T-0", file_id="T")
    else:
        hits = [_hit("enriched-T", thread_id="T")]
        sib = _hit("gmail-T1-body-0", thread_id="T")
    hits += [_hit(f"gmail-X{i}-body-0", thread_id=f"X{i}") for i in range(1, n)]
    if sibling_at is not None:
        hits[sibling_at] = sib
    return hits


def test_digest_is_dropped_when_a_raw_sibling_is_anywhere_in_the_pool():
    for at in (1, 12, 40, 59):
        out = _ids(_dedupe_by_cluster(_pool(sibling_at=at)))
        assert "enriched-T" not in out and "gmail-T1-body-0" in out
    out = _ids(_dedupe_by_cluster(_pool(sibling_at=40, drive=True)))
    assert "enriched-T" not in out and "gdrive-T-0" in out


def test_digest_is_kept_when_no_raw_sibling_was_retrieved():
    for drive in (False, True):
        assert _ids(_dedupe_by_cluster(_pool(drive=drive)))[0] == "enriched-T"


def test_two_raw_chunks_of_one_cluster_are_never_collapsed():
    hits = [_hit("gmail-T1-body-0", thread_id="T"), _hit("gmail-T2-body-0", thread_id="T")]
    assert _dedupe_by_cluster(hits) == hits


# -- prefix stability with digests in the store --------------------------------

def _seed_with_digests(tmp_path):
    """The prefix store plus digests: one whose raw file ranks deep in the
    pool (dropped regardless of limit), one with no raw chunk at all (kept)."""
    s = _seed(tmp_path)
    s.upsert_chunk("enriched-F69", "budget budget budget budget budget digest",
                   "hd69", {"file_id": "F69"})
    s.upsert_chunk("enriched-NORAW", "budget budget budget budget budget summary",
                   "hdnr", {"thread_id": "NORAW"})
    from mcpbrain.index import index_pending
    index_pending(s, _Emb())
    return s


def test_ranking_is_a_prefix_across_limits_with_digests(tmp_path):
    s = _seed_with_digests(tmp_path)
    for collapse in (False, True):
        r5, r10, r20 = (_ids(hybrid_search(s, _Emb(), "budget", limit=n,
                                           collapse_documents=collapse))
                        for n in (5, 10, 20))
        assert r10[:5] == r5, collapse
        assert r20[:10] == r10, collapse
        full = _ids(hybrid_search(s, _Emb(), "budget", limit=FUSION_DEPTH // 2,
                                  collapse_documents=collapse))
        assert "enriched-F69" not in full          # raw sibling in the pool
        assert "enriched-NORAW" in full            # nothing raw retrieved
