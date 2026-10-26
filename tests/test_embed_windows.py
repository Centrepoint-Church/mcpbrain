import hashlib
import logging

from mcpbrain.embed import (contextual_prefix, embed_chunk_passages,
                            passage_over_window)
from mcpbrain.index import EMBED_WINDOW_CHARS, index_pending
from mcpbrain.store import Store


class FakeEmbedder:
    dim = 4

    def __init__(self):
        self.calls: list[list[str]] = []

    def embed_passages(self, texts):
        self.calls.append(list(texts))
        out = []
        for t in texts:
            h = hashlib.sha256(t.encode()).digest()
            out.append([float(h[i]) + 1.0 for i in range(4)])
        return out

    def passages(self):
        return [t for call in self.calls for t in call]


MD = {"source_type": "gmail", "sender": "dana.okafor@example.org",
      "date": "2026-01-02", "subject": "Budget"}


def _norm(v):
    return sum(x * x for x in v) ** 0.5


def test_short_item_is_one_passage_and_batched():
    e = FakeEmbedder()
    items = [(f"short text {i}", MD) for i in range(5)]
    vecs = embed_chunk_passages(e, items, use_prefix=True)
    assert len(e.calls) == 1
    pre = contextual_prefix(MD)
    assert e.calls[0] == [pre + t for t, _ in items]
    assert vecs == e.embed_passages([pre + t for t, _ in items])


def test_long_text_split_across_windows_and_mean_normalised():
    e = FakeEmbedder()
    text = " ".join(f"word{i}" for i in range(900))[:5000]
    pre = contextual_prefix(MD)
    [vec] = embed_chunk_passages(e, [(text, MD)], use_prefix=True)
    sent = e.passages()
    assert len(sent) > 1
    assert all(len(p) <= EMBED_WINDOW_CHARS for p in sent)
    assert all(p.startswith(pre) for p in sent)
    assert "".join(p[len(pre):] for p in sent) == text
    assert abs(_norm(vec) - 1.0) < 1e-9
    pv = e.embed_passages(sent)
    w = [len(p) - len(pre) for p in sent]
    mean = [sum(wi * v[k] for v, wi in zip(pv, w)) / sum(w) for k in range(4)]
    n = _norm(mean)
    assert all(abs(a - b / n) < 1e-9 for a, b in zip(vec, mean))


def test_huge_prefix_is_truncated_so_text_keeps_200_chars_per_piece():
    e = FakeEmbedder()
    md = {"source_type": "gmail", "subject": "S" * 3000}
    text = "abcdefghij" * 300
    keep = EMBED_WINDOW_CHARS - 200
    assert len(contextual_prefix(md)) > keep
    [vec] = embed_chunk_passages(e, [(text, md)], use_prefix=True)
    sent = e.passages()
    assert all(len(p) <= EMBED_WINDOW_CHARS for p in sent)
    assert all(p[:keep] == contextual_prefix(md)[:keep] for p in sent)
    assert "".join(p[keep:] for p in sent) == text
    assert abs(_norm(vec) - 1.0) < 1e-9


def test_no_prefix_when_disabled():
    e = FakeEmbedder()
    text = "y" * 4500
    embed_chunk_passages(e, [(text, MD)], use_prefix=False)
    assert "".join(e.passages()) == text
    assert all(len(p) <= EMBED_WINDOW_CHARS for p in e.passages())


def test_index_pending_long_chunk_no_warning_and_writes_vector(tmp_path, caplog):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("d1", "z" * 5000, "h1", {"source_type": "gmail"})
    with caplog.at_level(logging.WARNING):
        n = index_pending(s, FakeEmbedder(), home=str(tmp_path))
    assert n == 1
    assert s.unembedded_chunks() == []
    assert "exceed" not in caplog.text
    with s._connect() as db:
        assert db.execute("SELECT count(*) FROM vec_chunks").fetchone()[0] == 1


def _prose(n_chars):
    words = " ".join(f"w{i:04d}" for i in range(n_chars))
    return words[:n_chars]


def test_just_over_window_splits_into_near_equal_pieces_no_tiny_tail():
    e = FakeEmbedder()
    text = _prose(2050)
    embed_chunk_passages(e, [(text, MD)], use_prefix=False)
    sent = e.passages()
    assert len(sent) == 2
    assert all(len(p) <= EMBED_WINDOW_CHARS for p in sent)
    assert "".join(sent) == text
    # Greedy would give ~1,995 + ~55; even split gives ~1,025 each.
    assert abs(len(sent[0]) - len(sent[1])) <= 10


def test_even_split_with_prefix_every_piece_fits_and_near_equal():
    e = FakeEmbedder()
    pre = contextual_prefix(MD)
    text = _prose(2 * EMBED_WINDOW_CHARS - len(pre))
    embed_chunk_passages(e, [(text, MD)], use_prefix=True)
    sent = e.passages()
    assert len(sent) == 3
    assert all(len(p) <= EMBED_WINDOW_CHARS for p in sent)
    assert "".join(p[len(pre):] for p in sent) == text
    sizes = [len(p) - len(pre) for p in sent]
    assert max(sizes) - min(sizes) <= 10


def test_unbreakable_text_still_fits_every_piece():
    e = FakeEmbedder()
    text = "q" * 4100  # no separators: hard cuts
    embed_chunk_passages(e, [(text, MD)], use_prefix=False)
    sent = e.passages()
    assert "".join(sent) == text
    assert all(len(p) <= EMBED_WINDOW_CHARS for p in sent)
    assert len(sent) == 3


def test_mean_weighted_by_piece_text_length():
    class Pinned:
        """Vector depends only on whether the passage holds the marker."""
        def embed_passages(self, texts):
            return [[1.0, 0.0] if "MARK" in t else [0.0, 1.0] for t in texts]

    # The only seam forces unequal pieces: 601 chars then 1,500.
    text = "MARK" + "a" * 596 + " " + "b" * 1500
    [vec] = embed_chunk_passages(Pinned(), [(text, MD)], use_prefix=False)
    from mcpbrain.embed import _plan_passages
    passages, weights = _plan_passages(text, "", EMBED_WINDOW_CHARS)
    assert weights == [len(p) for p in passages] == [601, 1500]
    w1, w2 = weights
    exp = [w1 / (w1 + w2), w2 / (w1 + w2)]
    n = _norm(exp)
    assert abs(vec[0] - exp[0] / n) < 1e-9 and abs(vec[1] - exp[1] / n) < 1e-9
    assert vec[0] != vec[1]


def test_mixed_batch_regroups_fitting_and_oversize_items():
    e = FakeEmbedder()
    long_text = _prose(4500)
    items = [("short a", MD), (long_text, MD), ("short b", MD), (_prose(2100), MD)]
    vecs = embed_chunk_passages(e, items, use_prefix=True)
    assert len(e.calls) == 1 and len(vecs) == 4
    single = [embed_chunk_passages(FakeEmbedder(), [it], use_prefix=True)[0]
              for it in items]
    assert vecs == single
    pre = contextual_prefix(MD)
    assert vecs[0] == e.embed_passages([pre + "short a"])[0]
    assert vecs[2] == e.embed_passages([pre + "short b"])[0]


def test_passage_over_window_measures_prefixed_passage():
    pre = contextual_prefix(MD)
    text = "x" * (EMBED_WINDOW_CHARS - len(pre) + 1)
    assert passage_over_window(text, MD, use_prefix=True)
    assert not passage_over_window(text, MD, use_prefix=False)
    assert not passage_over_window("x" * (EMBED_WINDOW_CHARS - len(pre)), MD,
                                   use_prefix=True)
    assert passage_over_window("x" * 2001, {}, use_prefix=True)
