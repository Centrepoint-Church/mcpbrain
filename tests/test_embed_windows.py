import hashlib
import logging

from mcpbrain.embed import contextual_prefix, embed_chunk_passages
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
    mean = [sum(v[k] for v in pv) / len(pv) for k in range(4)]
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
