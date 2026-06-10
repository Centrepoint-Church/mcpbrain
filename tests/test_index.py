from mcpbrain.store import Store
from mcpbrain.index import index_pending


class FakeEmbedder:
    dim = 4

    def embed_passages(self, texts):
        return [[1.0, 0, 0, 0] for _ in texts]


class CapturingEmbedder:
    """Fake embedder that records the texts passed to embed_passages."""
    dim = 4

    def __init__(self):
        self.captured_texts: list[str] = []

    def embed_passages(self, texts):
        self.captured_texts.extend(texts)
        return [[1.0, 0, 0, 0] for _ in texts]


def test_index_pending_embeds_and_marks_done(tmp_path):
    s = Store(tmp_path / "b.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("d1", "budget review", "h1", {"source_type": "gmail"})
    n = index_pending(s, FakeEmbedder())
    assert n == 1
    assert s.unembedded_chunks() == []


def test_embed_doc_embeds_single_chunk(tmp_path):
    s = Store(tmp_path / "e.sqlite3", dim=4)
    s.init()
    s.upsert_chunk("one", "first chunk", "h1", {"source_type": "gmail"})
    s.upsert_chunk("two", "second chunk", "h2", {"source_type": "gmail"})

    assert s.embed_doc("one", FakeEmbedder()) is True
    pending = [c["doc_id"] for c in s.unembedded_chunks()]
    assert "one" not in pending  # embedded
    assert "two" in pending      # untouched


def test_embed_doc_missing_returns_false(tmp_path):
    s = Store(tmp_path / "f.sqlite3", dim=4)
    s.init()
    assert s.embed_doc("nope", FakeEmbedder()) is False


def test_index_pending_prepends_contextual_prefix(tmp_path):
    """Passage text fed to embed_passages must start with [Context: and contain the original chunk text."""
    s = Store(tmp_path / "c.sqlite3", dim=4)
    s.init()
    chunk_text = "Q1 budget review memo"
    meta = {
        "source_type": "gmail",
        "sender": "finance@example.com",
        "date": "2026-03-01",
        "subject": "Q1 Budget",
        "org": "Acme",
    }
    s.upsert_chunk("d2", chunk_text, "h2", meta)

    emb = CapturingEmbedder()
    index_pending(s, emb)

    assert len(emb.captured_texts) == 1
    passage = emb.captured_texts[0]
    assert passage.startswith("[Context:"), f"Expected contextual prefix, got: {passage!r}"
    assert chunk_text in passage, f"Original chunk text missing from passage: {passage!r}"


def test_index_pending_prefix_gmail_contains_sender(tmp_path):
    """The contextual prefix in the passage must include the sender."""
    s = Store(tmp_path / "d.sqlite3", dim=4)
    s.init()
    meta = {
        "source_type": "gmail",
        "sender": "alice@example.org",
        "date": "2026-04-15",
        "subject": "Staff Update",
        "org": "Acme",
    }
    s.upsert_chunk("d3", "staff meeting notes", "h3", meta)

    emb = CapturingEmbedder()
    index_pending(s, emb)

    passage = emb.captured_texts[0]
    assert "alice@example.org" in passage
